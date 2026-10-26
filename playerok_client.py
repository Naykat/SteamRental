import asyncio
import json
import logging
import time
from threading import Thread

import requests as std_requests

import config
import item_images
from playerokapi.account import Account
from playerokapi.enums import ItemDealStatuses, ItemStatuses, PriorityTypes, GameCategoryDataFieldTypes
from playerokapi.listener.listener import EventListener
from playerokapi.misc import PERSISTED_QUERIES, QUERIES
from utils import generate_user_agent, list_price

logger = logging.getLogger(__name__)

CS2_GAME_SLUG = "counter-strike-2"


async def load_credentials() -> dict:
    """Данные входа из БД: cookies и прокси задаются в боте, User-Agent генерируется один раз.

    UA не меняется между запусками — к нему привязан __ddg5_.
    """
    import database as db
    saved_cookies = await db.get_setting_raw("playerok_cookies")
    user_agent = await db.get_setting_raw("playerok_user_agent")
    if not user_agent:
        user_agent = generate_user_agent()
        await db.set_setting("playerok_user_agent", user_agent)
    return {
        "cookies": json.loads(saved_cookies) if saved_cookies else {},
        "user_agent": user_agent,
        "proxy": await db.get_setting_raw("playerok_proxy") or None,
    }


async def save_credentials(cookies: dict | None = None, proxy: str | None = None):
    import database as db
    if cookies is not None:
        await db.set_setting("playerok_cookies", json.dumps(cookies, ensure_ascii=False))
    if proxy is not None:
        await db.set_setting("playerok_proxy", proxy)


def _create_account(creds: dict) -> Account:
    """Создаёт экземпляр Account, обходя singleton-паттерн библиотеки."""
    kwargs = {"cookies": dict(creds["cookies"])}
    if creds.get("proxy"):
        kwargs["proxy"] = creds["proxy"]
    if creds.get("user_agent"):
        kwargs["user_agent"] = creds["user_agent"]
    instance = object.__new__(Account)
    Account.__init__(instance, **kwargs)
    return instance


class PlayerokClient:
    def __init__(self):
        self.acc: Account | None = None
        self.creds: dict | None = None
        self.listener: EventListener | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.on_event = None  # async-колбэк, принимает событие listener'а
        self.listener_alive = False
        self.last_event_at: float | None = None
        self.last_error: str | None = None
        self.auth_ok = False

    # ---------- синхронные вызовы в отдельном потоке ----------

    async def _call(self, func, *args):
        return await asyncio.to_thread(func, *args)

    @staticmethod
    def _login(creds: dict) -> Account:
        acc = _create_account(creds)
        acc.get()
        if not acc.id:
            raise ValueError("Playerok не вернул профиль — cookies недействительны")
        return acc

    async def connect(self, creds: dict) -> Account:
        self.acc = await self._call(self._login, creds)
        self.creds = creds
        self.auth_ok = True
        logger.info(f"Playerok: вошёл как {self.acc.username}")
        return self.acc

    async def check_credentials(self, creds: dict) -> str:
        """Проверяет данные входа на отдельном экземпляре. Возвращает никнейм."""
        acc = await self._call(self._login, creds)
        return acc.username

    async def apply_credentials(self, creds: dict) -> str:
        """Проверяет и применяет новые cookies/UA/прокси без перезапуска бота."""
        new_acc = await self._call(self._login, creds)
        if self.acc is None:
            self.acc, self.creds, self.auth_ok = new_acc, creds, True
            if self.loop and self.listener is None:
                self.start_listener(self.loop)
            return new_acc.username
        if self.acc.id and new_acc.id != self.acc.id:
            raise ValueError(f"Это другой аккаунт Playerok ({new_acc.username}), "
                             f"а бот работает с {self.acc.username}. Перезапустите бот, чтобы сменить аккаунт.")

        def _swap():
            acc = self.acc
            with acc._Account__request_lock:
                acc.cookies = dict(creds["cookies"])
                acc.user_agent = new_acc.user_agent
                acc.proxy = creds.get("proxy")
                acc._Account__proxy_string = new_acc._Account__proxy_string
                acc._refresh_clients()
            acc.get()
            # websocket переподключится уже с новыми заголовками
            ws = getattr(self.listener, "ws", None)
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
        await self._call(_swap)
        self.creds, self.auth_ok, self.last_error = creds, True, None
        logger.info("Playerok: cookies обновлены")
        return self.acc.username

    async def health_check(self) -> bool:
        if self.acc is None:
            return False
        try:
            await self._call(self.acc.get)
            self.auth_ok = bool(self.acc.id)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.auth_ok = False
        return self.auth_ok

    async def send_message(self, chat_id: str, text: str):
        try:
            await self._call(self.acc.send_message, chat_id, text)
        except Exception as e:
            logger.error(f"[playerok] не удалось отправить сообщение в {chat_id}: {e}")
            raise

    async def get_deal(self, deal_id: str):
        return await self._call(self.acc.get_deal, deal_id)

    async def get_paid_deals(self) -> list:
        """Оплаченные, но ещё не выполненные заказы."""
        def _get():
            page = self.acc.get_deals(statuses=[ItemDealStatuses.PAID], count=24)
            return list(getattr(page, "deals", None) or [])
        return await self._call(_get)

    async def confirm_deal(self, deal_id: str):
        await self._call(self.acc.update_deal, deal_id, ItemDealStatuses.SENT)

    async def get_profile(self) -> dict:
        def _get():
            self.acc.get()
            profile = self.acc.profile
            balance = profile.balance if profile else None
            stats = profile.stats if profile else None
            items = stats.items if stats else None
            deals_in = stats.deals.incoming if stats and stats.deals else None
            return {
                "username": self.acc.username,
                "total": getattr(balance, "value", 0) or 0,
                "available": getattr(balance, "available", 0) or 0,
                "withdrawable": getattr(balance, "withdrawable", 0) or 0,
                "frozen": getattr(balance, "frozen", 0) or 0,
                "pending_income": getattr(balance, "pending_income", 0) or 0,
                "rating": getattr(profile, "rating", None),
                "reviews": getattr(profile, "reviews_count", None),
                "items_total": getattr(items, "total", None),
                "items_finished": getattr(items, "finished", None),
                "sales_total": getattr(deals_in, "total", None),
                "sales_finished": getattr(deals_in, "finished", None),
                "unread_chats": self.acc.unread_chats_counter,
                "is_blocked": self.acc.is_blocked,
                "can_publish": self.acc.can_publish_items,
            }
        return await self._call(_get)

    # ---------- перевыставление лота ----------

    async def relist_same(self, item_id: str, premium: bool, premium_fallback: bool,
                          template: dict | None = None) -> dict:
        return await self._call(self._relist_same, item_id, premium, premium_fallback, template or {})

    def _relist_same(self, item_id, premium, premium_fallback, template) -> dict:
        """Выставляет снова тот же лот: обновляет комментарий (новый пароль) и правки товара,
        затем публикует. ID лота не меняется. Бросает исключение, если Playerok не даёт."""
        acc = self.acc
        acc.get()  # обновить баланс
        lot = acc.get_item(id=item_id)
        if lot is None:
            raise ValueError("лот не найден на Playerok")

        # Правки товара, сделанные во время аренды, + комментарий с новым паролем
        changes = {"comment": template.get("comment")}
        if template.get("name") and template["name"] != lot.name:
            changes["name"] = template["name"]
        if template.get("description") and template["description"] != lot.description:
            changes["description"] = template["description"]
        if template.get("price") and template["price"] != lot.price:
            changes["price"] = template["price"]
        attrs = template.get("attributes") or {}
        if attrs and any((lot.attributes or {}).get(k) != v for k, v in attrs.items()):
            changes["attributes"] = attrs

        def update():
            return self._update_lot(item_id, changes.get("name"), changes.get("description"),
                                    changes.get("price"), changes.get("attributes"), None,
                                    changes.get("comment"), allow_sold=True)

        updated_before = False
        try:
            update()
            updated_before = True
        except Exception as e:
            logger.warning(f"[relist] проданный лот {item_id} не обновился до публикации: {e}")

        price = changes.get("price") or lot.price
        if lot.status == ItemStatuses.APPROVED:
            result = {"published": True, "item_id": lot.id, "slug": lot.slug, "price": price,
                      "premium": False, "premium_price": 0, "warning": None}
        else:
            result = self._publish(lot, price, premium, premium_fallback)
        if not updated_before:
            try:
                update()
            except Exception as e:
                result["comment_error"] = str(e)
        result.update(same=True, item_id=lot.id, slug=getattr(lot, "slug", "") or result.get("slug", ""))
        return result

    async def republish(self, item_id: str, premium: bool, premium_fallback: bool,
                        replace: dict[str, str] | None = None, template: dict | None = None) -> dict:
        return await self._call(self._republish, item_id, premium, premium_fallback,
                                replace or {}, template or {})

    def _republish(self, item_id, premium, premium_fallback, replace, template) -> dict:
        """Создаёт копию лота item_id и выставляет её на продажу.

        replace — подстановки в описании/полях (например, старый пароль -> новый).
        template — сохранённые в боте данные товара (name, description, price, attributes, images);
        то, чего в нём нет, берётся из исходного лота.
        """
        acc = self.acc
        acc.get()  # обновить баланс
        try:
            old = acc.get_item(id=item_id)
        except Exception as e:
            logger.warning(f"[republish] исходный лот {item_id} недоступен: {e}")
            old = None
        if old is not None and old.status == ItemStatuses.APPROVED:
            return {"skipped": True, "reason": "исходный лот ещё активен"}

        def sub(text):
            text = text or ""
            for src, dst in replace.items():
                if src:
                    text = text.replace(src, dst)
            return text

        attrs = template.get("attributes") or {}
        if template.get("name") and template.get("description") and template.get("price") and attrs.get("amount"):
            # Все данные товара есть в боте — создаём лот с нуля, исходный лот не нужен
            images = list(template.get("images") or []) or self._download_attachments(old)
            if not images:
                raise ValueError("Нет картинок для лота — добавьте их в «✏️ Редактировать товар»")
            result = self._create_rental_item(
                template["name"], sub(template["description"]), template["price"], attrs["amount"],
                attrs.get("prime-status", False), images, template.get("comment") or "",
                premium, premium_fallback,
            )
            result.update(old_slug=getattr(old, "slug", None), name=template["name"])
            return result

        if old is None or not getattr(old, "category", None) or not getattr(old, "obtaining_type", None):
            raise ValueError("Не удалось получить исходный лот")

        data_fields = []
        source_fields = old.data_fields
        if source_fields is None:
            source_fields = acc.get_game_category_data_fields(old.category.id, old.obtaining_type.id).data_fields
        for field in source_fields or []:
            if field.type != GameCategoryDataFieldTypes.ITEM_DATA:
                continue
            if template.get("comment") and _is_comment_field(field):
                field.value = template["comment"]
            else:
                field.value = sub(field.value)
            data_fields.append(field)

        attachments = list(template.get("images") or []) or self._download_attachments(old)

        name = template.get("name") or old.name
        # price — фактическая цена (со скидкой), raw_price — цена до скидки
        price = template.get("price") or old.price or old.raw_price
        start_price = list_price(price)
        attributes = {**(old.attributes or {}), **(template.get("attributes") or {})}
        created = acc.create_item(
            game_category_id=old.category.id,
            obtaining_type_id=old.obtaining_type.id,
            name=name,
            price=start_price,
            description=sub(template.get("description") or old.description),
            data_fields=data_fields,
            attachments=attachments,
            attributes=attributes,
        )

        result = self._publish(created, start_price, premium, premium_fallback)
        result.setdefault("item_id", created.id)
        self._apply_discount(result["item_id"], start_price, price, result)
        result.update(old_slug=old.slug, name=name)
        return result

    @staticmethod
    def _download_attachments(lot) -> list[bytes]:
        result = []
        for file in getattr(lot, "attachments", None) or []:
            try:
                r = std_requests.get(file.url, timeout=30)
                r.raise_for_status()
                if item_images.is_valid(r.content):
                    result.append(r.content)
            except Exception as e:
                logger.warning(f"[republish] не удалось скачать картинку {file.url}: {e}")
        return result

    async def remove_lot(self, item_id: str):
        await self._call(self.acc.remove_item, item_id)

    # ---------- редактирование лота ----------

    async def update_lot(self, item_id: str, name: str | None = None, description: str | None = None,
                         price: int | None = None, attributes: dict | None = None,
                         images: list[bytes] | None = None, comment: str | None = None) -> dict:
        return await self._call(self._update_lot, item_id, name, description, price, attributes, images, comment)

    def _update_lot(self, item_id, name, description, price, attributes, images, comment=None,
                    allow_sold=False) -> dict:
        """Обновляет лот на Playerok, если он сейчас выставлен.

        Свой запрос вместо Account.update_item: там новые картинки мапятся в
        variables.attachments, а мутация принимает addedAttachments.
        """
        acc = self.acc
        lot = acc.get_item(id=item_id)
        if lot is None:
            return {"updated": False, "reason": "лот не найден на Playerok"}
        if not allow_sold and lot.status in (ItemStatuses.SOLD, ItemStatuses.EXPIRED, ItemStatuses.BLOCKED):
            return {"updated": False, "reason": "лот сейчас не выставлен — изменения попадут в следующий"}

        payload_input = {"id": item_id}
        if name:
            payload_input["name"] = name
        if description:
            payload_input["description"] = description
        if price:
            payload_input["price"] = int(price)
        if attributes:
            payload_input["attributes"] = {**(lot.attributes or {}), **attributes}
        if images:
            payload_input["removedAttachments"] = [f.id for f in (lot.attachments or []) if getattr(f, "id", None)]
        if comment is not None:
            fields = lot.data_fields
            if fields is None:
                fields = acc.get_game_category_data_fields(lot.category.id, lot.obtaining_type.id).data_fields
            fields = [f for f in fields or [] if f.type == GameCategoryDataFieldTypes.ITEM_DATA]
            target = [f for f in fields if _is_comment_field(f)]
            if not target:
                if len(payload_input) == 1:
                    return {"updated": False, "reason": "в лоте нет поля комментария"}
            elif all((f.value or "") == comment for f in target) and len(payload_input) == 1:
                return {"updated": False, "reason": "комментарий уже актуален", "unchanged": True}
            else:
                payload_input["dataFields"] = [
                    {"fieldId": f.id, "value": comment if _is_comment_field(f) else (f.value or "")}
                    for f in fields
                ]
        if len(payload_input) == 1:
            return {"updated": False, "reason": "нечего обновлять", "unchanged": True}

        operations = {
            "operationName": "updateItem",
            "query": QUERIES.get("updateItem"),
            "variables": {"input": payload_input, "addedAttachments": [None] * len(images) if images else None},
        }
        headers = {"accept": "*/*"}
        if images:
            files, file_map = {}, {}
            for i, data in enumerate(images, start=1):
                filename, file_obj, content_type = acc._resolve_image_file(data)
                file_map[str(i)] = [f"variables.addedAttachments.{i - 1}"]
                files[str(i)] = (filename, file_obj, content_type)
            payload = {"operations": json.dumps(operations), "map": json.dumps(file_map)}
            r = acc.request("post", f"{acc.base_url}/graphql", headers, payload, files).json()
        else:
            r = acc.request("post", f"{acc.base_url}/graphql", headers, operations).json()
        updated = r["data"]["updateItem"] or {}
        return {"updated": True, "status": updated.get("status"), "slug": updated.get("slug") or lot.slug}

    async def get_lot(self, item_id: str):
        return await self._call(lambda: self.acc.get_item(id=item_id))

    def _publish(self, created, price, premium, premium_fallback) -> dict:
        """Публикует созданный черновик с нужным статусом приоритета."""
        acc = self.acc
        statuses = acc.get_item_priority_statuses(created.id, price)
        by_type = {s.type: s for s in statuses}
        target = by_type.get(PriorityTypes.DEFAULT) or (statuses[0] if statuses else None)
        default_status = target
        used_premium, warning = False, None
        if premium:
            premium_status = by_type.get(PriorityTypes.PREMIUM)
            # Премиум оплачивается целиком либо из свободного, либо из замороженного баланса —
            # суммировать их нельзя
            balance = acc.profile.balance if acc.profile else None
            available = getattr(balance, "available", 0) or 0
            frozen = getattr(balance, "frozen", 0) or 0
            if not premium_status:
                warning = "премиум недоступен для этого лота"
            elif premium_status.price and max(available, frozen) < premium_status.price:
                warning = (f"не хватает баланса на премиум {premium_status.price} ₽: "
                           f"свободно {available} ₽, заморожено {frozen} ₽")
            else:
                target, used_premium = premium_status, True
            if warning and not premium_fallback:
                return {"created_id": created.id, "published": False, "warning": warning,
                        "slug": getattr(created, "slug", ""), "price": price}
        if not target:
            raise ValueError("Нет доступных статусов публикации")

        try:
            published = acc.publish_item(created.id, target.id)
        except Exception as e:
            # Окончательно решает Playerok: если он не принял оплату премиума — обычное выставление
            if not used_premium or not default_status:
                raise
            logger.warning(f"[publish] Playerok отклонил премиум для {created.id}: {e}")
            warning = f"Playerok отклонил премиум: {e}"
            if not premium_fallback:
                return {"created_id": created.id, "published": False, "warning": warning,
                        "slug": getattr(created, "slug", ""), "price": price}
            target, used_premium = default_status, False
            published = acc.publish_item(created.id, target.id)
        return {
            "published": True,
            "item_id": published.id,
            "slug": getattr(published, "slug", "") or getattr(created, "slug", ""),
            "price": price,
            "premium": used_premium,
            "premium_price": target.price if used_premium else 0,
            "warning": warning,
        }

    # ---------- новый лот аренды CS2 ----------

    async def create_rental_item(self, title: str, description: str, price: int, hours: int,
                                 prime: bool, images: list[bytes], comment: str,
                                 premium: bool, premium_fallback: bool) -> dict:
        return await self._call(self._create_rental_item, title, description, price, hours,
                                prime, images, comment, premium, premium_fallback)

    def _create_rental_item(self, title, description, price, hours, prime, images, comment,
                            premium, premium_fallback) -> dict:
        images = [img for img in images if item_images.is_valid(img)]
        if not images:
            raise ValueError("Нет целых картинок для лота — загрузите их в «✏️ Редактировать товар» → 🖼 Картинки")
        acc = self.acc
        acc.get()  # обновить баланс
        game = acc.get_game(slug=CS2_GAME_SLUG)
        category = next((c for c in game.categories if "аренд" in c.name.lower()), None)
        if not category:
            raise ValueError("На Playerok не найдена категория аренды CS2")
        obtaining = acc.get_game_category_obtaining_types(category.id).obtaining_types
        if not obtaining:
            raise ValueError("Нет доступных типов получения для категории аренды")
        obtaining_type = obtaining[0]

        data_fields = []
        for field in acc.get_game_category_data_fields(category.id, obtaining_type.id).data_fields:
            if field.type != GameCategoryDataFieldTypes.ITEM_DATA:
                continue
            if _is_comment_field(field) or (getattr(field, "required", False) and not field.value):
                field.value = comment
            elif not field.value:
                field.value = ""
            data_fields.append(field)

        start_price = list_price(price)
        created = acc.create_item(
            game_category_id=category.id,
            obtaining_type_id=obtaining_type.id,
            name=title,
            price=start_price,
            description=description,
            data_fields=data_fields,
            attachments=images,
            attributes={"amount": hours, "prime-status": bool(prime)},
        )
        result = self._publish(created, start_price, premium, premium_fallback)
        result.setdefault("item_id", created.id)
        self._apply_discount(result["item_id"], start_price, price, result)
        return result

    def _apply_discount(self, item_id: str, start_price: int, price: int, result: dict):
        """Скидка: лот выставлен по цене-ступени (499/999/2499), меняем её на желаемую по ID лота."""
        result["price"] = start_price
        if start_price == price:
            return
        try:
            self._set_price(item_id, price)
            result.update(price=price, list_price=start_price)
        except Exception as e:
            logger.error(f"[price] не удалось поставить цену {price} лоту {item_id}: {e}")
            result["discount_error"] = str(e)

    def _set_price(self, item_id: str, price: int):
        acc = self.acc
        payload = {
            "operationName": "updateItem",
            "query": QUERIES.get("updateItem"),
            "variables": {"input": {"id": item_id, "price": int(price)}, "addedAttachments": None},
        }
        acc.request("post", f"{acc.base_url}/graphql", {"accept": "*/*"}, payload)

    # ---------- вывод средств ----------

    async def get_withdraw_provider(self) -> dict | None:
        """Возвращает сырые данные провайдера вывода (WITHDRAW_PROVIDER)."""
        def _get():
            payload = {
                "operationName": "transactionProviders",
                "variables": json.dumps({"filter": {"direction": "OUT"}}),
                "extensions": json.dumps({"persistedQuery": {
                    "version": 1, "sha256Hash": PERSISTED_QUERIES.get("transactionProviders"),
                }}),
            }
            r = self.acc.request("get", f"{self.acc.base_url}/graphql", {"accept": "*/*"}, payload).json()
            providers = r["data"]["transactionProviders"] or []
            for provider in providers:
                if provider.get("id") == config.WITHDRAW_PROVIDER:
                    return provider
            return {"_missing": True, "available": [p.get("id") for p in providers]}
        return await self._call(_get)

    @staticmethod
    def pick_payment_method(provider: dict) -> str | None:
        if config.WITHDRAW_PAYMENT_METHOD:
            return config.WITHDRAW_PAYMENT_METHOD
        for method in provider.get("paymentMethods") or []:
            text = f"{method.get('id', '')} {method.get('name', '')}".upper()
            if any(k in text for k in ("TRC", "TRON", "TRX")):
                return method.get("id")
        return None

    async def request_withdrawal(self, wallet: str, amount: int, payment_method_id: str | None) -> dict:
        def _req():
            payload = {
                "operationName": "requestWithdrawal",
                "query": QUERIES.get("requestWithdrawal"),
                "variables": {"input": {
                    "provider": config.WITHDRAW_PROVIDER,
                    "account": wallet,
                    "value": int(amount),
                    "providerData": {"paymentMethodId": payment_method_id, "sbpBankMemberId": None},
                }},
            }
            r = self.acc.request("post", f"{self.acc.base_url}/graphql", {"accept": "*/*"}, payload).json()
            return r["data"]["requestWithdrawal"]
        return await self._call(_req)

    # ---------- слушатель событий ----------

    def start_listener(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        Thread(target=self._listen_forever, daemon=True, name="playerok-listener").start()

    def _listen_forever(self):
        while True:
            try:
                self.listener_alive = True
                self.listener = listener = EventListener(self.acc)
                for event in listener.listen(get_new_message_events=True, get_new_review_events=False):
                    self.last_event_at = time.time()
                    if self.on_event:
                        future = asyncio.run_coroutine_threadsafe(self.on_event(event), self.loop)
                        future.add_done_callback(_log_future_error)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception("Listener Playerok упал, перезапуск через 15 сек")
            self.listener_alive = False
            time.sleep(15)
            try:
                self.acc.get()
            except Exception as e:
                logger.error(f"Не удалось переподключиться к Playerok: {e}")


def _is_comment_field(field) -> bool:
    return "коммент" in (getattr(field, "label", "") or "").lower()


def _log_future_error(future):
    exc = future.exception()
    if exc:
        logger.error("Ошибка обработки события Playerok", exc_info=exc)


client = PlayerokClient()
