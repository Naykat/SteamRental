import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

# Cookies и прокси Playerok задаются в Telegram-боте (🍪 Cookies), User-Agent генерируется сам

DB_PATH = os.path.join(BASE_DIR, "rental.db")
# Картинки лотов: images/<id аккаунта>/1.jpg ...
IMAGES_DIR = os.path.join(BASE_DIR, "images")

# Автовыполнение заказа: после выдачи аккаунта заказ сразу отмечается выполненным
AUTO_CONFIRM_DEAL = _bool("AUTO_CONFIRM_DEAL", True)
# Как часто сверять оплаченные заказы через API (подстраховка к websocket), сек
DEALS_POLL_SECONDS = int(os.getenv("DEALS_POLL_SECONDS", "60"))
# ТЕСТ: если > 0, любая аренда длится столько минут с момента покупки (часы лота игнорируются).
# Для работы поставьте 0
TEST_RENTAL_MINUTES = int(os.getenv("TEST_RENTAL_MINUTES", "0"))
# Пароль Steam меняется после каждой аренды; если не вышло — повтор через N минут
ROTATION_RETRY_MINUTES = int(os.getenv("ROTATION_RETRY_MINUTES", "10"))
# Длительность аренды по умолчанию, если её не удалось определить из лота
DEFAULT_RENTAL_HOURS = int(os.getenv("DEFAULT_RENTAL_HOURS", "1"))
# Бонусные минуты сверх оплаченного времени (на вход в аккаунт)
BONUS_MINUTES = int(os.getenv("BONUS_MINUTES", "10"))
# За сколько минут до конца предупредить покупателя (0 — не предупреждать)
WARN_BEFORE_MINUTES = int(os.getenv("WARN_BEFORE_MINUTES", "10"))
# Режим выставления по умолчанию (дальше меняется в боте)
PUBLISH_PREMIUM = _bool("PUBLISH_PREMIUM", False)
# Провайдер вывода Playerok для TRX/TRC20-кошелька и (опционально) ID платёжного метода
WITHDRAW_PROVIDER = os.getenv("WITHDRAW_PROVIDER", "USDT")
WITHDRAW_PAYMENT_METHOD = os.getenv("WITHDRAW_PAYMENT_METHOD", "")
# Минимальный интервал между запросами !код от одного чата, сек
CODE_COOLDOWN_SECONDS = int(os.getenv("CODE_COOLDOWN_SECONDS", "10"))

CODE_COMMANDS = {"!код", "!code", "!guard", "!гуард"}
TIME_COMMANDS = {"!время", "!time", "!осталось"}

CREDENTIALS_TEXT = (
    "Спасибо за покупку!\n\n"
    "Логин: {login}\n"
    "Пароль: {password}\n\n"
    "Аренда до: {ends_at} (МСК)\n\n"
    "Чтобы получить код Steam Guard, напишите в этот чат: !код\n"
    "Узнать оставшееся время: !время"
)
# Ответ на !код и !время. Если в чате несколько аренд, перед каждой строкой ставится логин
CODE_TEXT = "{code}"
TIME_TEXT = "Осталось {left}"
NO_FREE_ACCOUNTS_TEXT = (
    "Спасибо за покупку! Сейчас все аккаунты заняты — продавец уже уведомлён "
    "и скоро выдаст данные вручную."
)
WARN_TEXT = "До окончания аренды аккаунта {login} осталось {minutes} мин."
EXPIRED_TEXT = "Аренда аккаунта завершена. Спасибо, что выбрали нас! Будем рады вашему отзыву."
# Описание лота по умолчанию (кнопка «Стандартное» при создании товара)
DEFAULT_ITEM_DESCRIPTION = (
    "Аренда аккаунта Steam с CS2.\n\n"
    "• Данные для входа приходят в чат автоматически сразу после оплаты.\n"
    "• Код Steam Guard — командой !код в чате, оставшееся время — !время.\n"
    "• Запрещено менять данные аккаунта, привязывать почту/телефон, читерить."
)
# Комментарий к лоту — его покупатель видит после оплаты. {login} и {password} подставляются
ITEM_COMMENT = (
    "Логин: {login}\n"
    "Пароль: {password}\n\n"
    'На аккаунте установлен Steam Guard. Чтобы получить код напишите "!код" в чате'
)
NO_RENTAL_TEXT = "У вас нет активной аренды в этом чате."
