import os
import re
import html
import logging
import asyncio
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "@AliPriceHunterBot").strip()
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "قناة العروض").strip()
CHANNEL_URL = os.getenv(
    "CHANNEL_URL",
    "https://t.me/HunterAliExpressDZ",
).strip()

try:
    OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0").strip())
except ValueError:
    OWNER_CHAT_ID = 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ALIEXPRESS_URL = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*aliexpress\.com/[^\s]+",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)


def clean(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_product_title(value: str) -> str:
    """إزالة لاحقة AliExpress التي قد تظهر في نهاية عنوان المنتج."""
    title = clean(value)
    title = re.sub(
        r"\s*(?:[-|–—:]\s*)?AliExpress(?:\s*[-|–—:]?\s*\d+)?\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return title.strip(" -|–—:") or "منتج من AliExpress"


def choose_english_title(soup: BeautifulSoup, page: str) -> str:
    """اختيار العنوان الإنجليزي الأصلي من الصفحة، لا ترجمته آلياً."""
    candidates = [
        tag.get_text(" ", strip=True)
        for tag in soup.find_all(["h1", "h2"])
        if tag.get_text(" ", strip=True)
    ]
    candidates.extend(
        [
            meta(soup, "og:title"),
            meta(soup, "twitter:title"),
            clean(soup.title.get_text(" ", strip=True) if soup.title else ""),
        ]
    )
    for key in ("title", "productTitle", "subject"):
        match = re.search(
            rf'"{key}"\s*:\s*"([^"\\]{{5,300}})"',
            page,
            re.IGNORECASE,
        )
        if match:
            candidates.append(match.group(1))

    for candidate in candidates:
        candidate = clean_product_title(candidate)
        if candidate and not re.search(r"[\u0600-\u06ff]", candidate):
            return candidate
    return clean_product_title(candidates[0]) if candidates else "منتج من AliExpress"


def get_url(text: str) -> Optional[str]:
    match = ALIEXPRESS_URL.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,،؛)>]")


def meta(soup: BeautifulSoup, name: str) -> str:
    for attribute in ("property", "name"):
        tag = soup.find("meta", attrs={attribute: name})
        if tag and tag.get("content"):
            return clean(str(tag["content"]))
    return ""


def find_image(soup: BeautifulSoup, page: str) -> str:
    image = meta(soup, "og:image") or meta(soup, "twitter:image")

    if not image:
        tag = soup.find("meta", attrs={"itemprop": "image"})
        if tag and tag.get("content"):
            image = str(tag["content"])

    if not image:
        match = re.search(
            r'"(?:image|imageUrl|productImage|mainImage|images)"\s*:\s*(?:\[\s*)?"(https?:\\?/\\?/[^" ]+)"',
            page,
            re.IGNORECASE,
        )
        if match:
            image = match.group(1)

    if not image:
        for tag in soup.find_all("img"):
            candidate = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
            if candidate and str(candidate).startswith("http"):
                image = str(candidate)
                break

    return image.replace("\\u002F", "/").replace("\\/", "/").strip()


def read_product(url: str) -> dict:
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()

    page = response.text
    soup = BeautifulSoup(page, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    title = choose_english_title(soup, page)

    return {
        "title": title,
        "image": find_image(soup, page),
    }


def caption(product: dict, url: str) -> str:
    title = html.escape(product.get("title", "غير متوفر"))
    visible_url = html.escape(url)
    link_url = html.escape(url, quote=True)
    bot = html.escape(BOT_USERNAME)
    channel_name = html.escape(CHANNEL_NAME)
    channel_url = html.escape(CHANNEL_URL, quote=True)

    # السعر والقسائم حقول يملؤها المستخدم يدوياً بعد نسخ النص.
    return f"""✅ {title}

💰 <b>سعر التخفيض:</b>
ضع السعر هنا

🎟 <b>كوبون المنصة:</b>
`ضع الكود هنا`

🏷 <b>قسيمة البائع:</b>
`ضع الكود هنا`

🛒 <b>رابط الشراء:</b>
<a href="{link_url}">{visible_url}</a>

📢 <b>{channel_name}:</b>
<a href="{channel_url}">{channel_url}</a>

    🤖 <b>البوت:</b> {bot}"""


class HealthHandler(BaseHTTPRequestHandler):
    """نقطة فحص بسيطة حتى تتعرف خدمة Render على المنفذ المفتوح."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format: str, *args: object) -> None:
        # منع ظهور طلبات الفحص في السجل كل مرة.
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server listening on port %s", port)
    server.serve_forever()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != OWNER_CHAT_ID:
        return
    await update.message.reply_text("أرسل رابط منتج AliExpress.")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    if update.effective_chat.id != OWNER_CHAT_ID:
        await update.message.reply_text("هذا البوت خاص بالمالك فقط.")
        return

    url = get_url(update.message.text or "")
    if not url:
        await update.message.reply_text("أرسل رابط AliExpress صحيحاً.")
        return

    status = await update.message.reply_text("جاري استخراج الصورة والوصف...")

    try:
        product = read_product(url)
        text = caption(product, url)
        chat_id = update.effective_chat.id

        if product["image"]:
            try:
                image_response = requests.get(
                    product["image"],
                    headers={"User-Agent": USER_AGENT},
                    timeout=20,
                )
                image_response.raise_for_status()
                image = BytesIO(image_response.content)
                image.name = "product.jpg"
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image,
                    caption=text,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("فشل إرسال الصورة، سيتم إرسال النص فقط")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )

        await status.edit_text("تم تجهيز المنتج.")

    except requests.RequestException:
        logger.exception("فشل فتح رابط AliExpress")
        await status.edit_text("تعذر فتح رابط AliExpress. جرّب الرابط مرة أخرى.")
    except Exception:
        logger.exception("خطأ غير متوقع")
        await status.edit_text("حدث خطأ. راجع نافذة التشغيل لمعرفة التفاصيل.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("ضع TELEGRAM_BOT_TOKEN في ملف .env")
    if not OWNER_CHAT_ID:
        raise RuntimeError("ضع OWNER_CHAT_ID الصحيح في ملف .env")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    # Web Service المجاني في Render يتطلب منفذاً مفتوحاً.
    # الخادم يعمل في مسار منفصل بينما يستمر البوت باستخدام polling.
    Thread(target=start_health_server, daemon=True).start()

    logger.info("Bot is running...")
    # Python 3.14 لا ينشئ event loop تلقائياً في MainThread.
    # إنشاء الحلقة هنا يمنع خطأ get_event_loop في Render.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()




