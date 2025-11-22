#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات دانلودر ویدیو تلگرام - نسخه بهبود یافته
بدون دیتابیس و با دانلود مستقیم به حافظه
"""

import os
import re
import json
import logging
import time
import threading
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone
from collections import defaultdict, deque
from html import unescape
from collections import defaultdict, deque

import yt_dlp
import requests
from bs4 import BeautifulSoup
import telebot
from telebot import types
import urllib3
from moviepy.editor import VideoFileClip

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# سشن اشتراکی برای دانلودهای بسیار سریع
DOWNLOAD_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=3)
FAST_SESSION = requests.Session()
FAST_SESSION.mount('https://', DOWNLOAD_ADAPTER)
FAST_SESSION.mount('http://', DOWNLOAD_ADAPTER)

# توکن ربات
BOT_TOKEN = "8414466743:AAFeOrB3ElfZKrashXksHjGaqllHdpGUn3U"

# ایجاد ربات با timeout بیشتر
bot = telebot.TeleBot(BOT_TOKEN)
# تنظیم timeout برای ارسال فایل‌های بزرگ
import telebot.apihelper
telebot.apihelper.READ_TIMEOUT = 300  # 5 دقیقه برای فایل‌های بزرگ

# پوشه موقت
TEMP_DIR = Path("temp_videos")
TEMP_DIR.mkdir(exist_ok=True)

# مسیر ذخیره‌سازی تاریخچه (بدون دیتابیس)
STORAGE_PATH = Path("request_history.json")
MAX_HISTORY_PER_USER = 200
REQUEST_HISTORY = defaultdict(lambda: deque())

# مسیر و وضعیت مدیریت
MANAGER_STATE_PATH = Path("manager_state.json")
_MANAGER_STATE_CACHE = {"mtime": None, "data": {}}

# مسیر فایل کاربران مشترک
USERS_DATA_PATH = Path("data/users.json")

# ذخیره URL ویدیو برای استخراج آهنگ (message_id -> video_url)
VIDEO_URL_CACHE = {}


def write_temp_video(video_data: BytesIO, destination: Path) -> Path:
    """ذخیره ویدیو در مسیر موقت مشخص"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    video_data.seek(0)
    with open(destination, 'wb') as temp_file:
        temp_file.write(video_data.read())
    video_data.seek(0)
    return destination


def sanitize_caption(text: Optional[str]) -> Optional[str]:
    """پاکسازی کپشن از HTML و محدود کردن طول"""
    if not text or not isinstance(text, str):
        return None
    text = unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('\r', '\n')
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    if not text:
        return None
    if len(text) > 1024:
        text = text[:1021] + '...'
    return text


def resolve_caption(metadata: Optional[dict], url: str) -> Optional[str]:
    """تلاش برای یافتن کپشن نهایی با چند منبع"""
    if metadata is None:
        metadata = {}
    direct_caption = sanitize_caption(metadata.get('caption_original'))
    if direct_caption:
        return direct_caption
    candidates = [
        metadata.get('caption'),
        metadata.get('description'),
        metadata.get('title'),
        metadata.get('summary'),
    ]
    for candidate in candidates:
        cleaned = sanitize_caption(candidate)
        if cleaned:
            return cleaned
    
    # تلاش مجدد برای استخراج کپشن از اینستاگرام در صورت نبود
    fallback = extract_instagram_caption(url)
    return sanitize_caption(fallback)


def load_request_history():
    """لود تاریخچه از فایل JSON"""
    REQUEST_HISTORY.clear()
    if not STORAGE_PATH.exists():
        logger.info("ℹ️ فایل تاریخچه یافت نشد؛ شروع با حافظه خالی.")
        return
    
    try:
        with open(STORAGE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        restored_users = 0
        for user_id_str, entries in data.items():
            try:
                user_id = int(user_id_str)
            except ValueError:
                continue
            history = REQUEST_HISTORY[user_id]
            for entry in entries[:MAX_HISTORY_PER_USER]:
                url_value = entry.get('url')
                if isinstance(url_value, str) and 'instagram.com' not in url_value.lower():
                    continue
                created_at = entry.get('created_at')
                if isinstance(created_at, str):
                    try:
                        normalized = created_at.replace('Z', '+00:00')
                        entry['created_at'] = datetime.fromisoformat(normalized)
                    except ValueError:
                        entry['created_at'] = datetime.now(timezone.utc)
                else:
                    entry['created_at'] = datetime.now(timezone.utc)
                history.append(entry)
            restored_users += 1
        logger.info(f"✅ تاریخچه {restored_users} کاربر لود شد.")
    except Exception as e:
        logger.error(f"❌ خطا در لود تاریخچه: {e}")
        REQUEST_HISTORY.clear()


def save_request_history():
    """ذخیره تاریخچه در فایل JSON"""
    try:
        serializable = {
            str(user_id): [
                {
                    **entry,
                    'created_at': entry['created_at'].isoformat()
                    if hasattr(entry['created_at'], 'isoformat')
                    else datetime.now(timezone.utc).isoformat()
                }
                for entry in history
            ]
            for user_id, history in REQUEST_HISTORY.items()
        }
        with open(STORAGE_PATH, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره تاریخچه: {e}")


def init_storage():
    """راه‌اندازی ذخیره‌سازی بدون دیتابیس"""
    load_request_history()
    # اطمینان از وجود فایل users.json
    USERS_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not USERS_DATA_PATH.exists():
        with open(USERS_DATA_PATH, 'w', encoding='utf-8') as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    logger.info("✅ حالت بدون دیتابیس با ذخیره‌سازی فایل فعال شد")


def load_users() -> dict:
    """لود لیست کاربران از فایل JSON"""
    if not USERS_DATA_PATH.exists():
        return {}
    try:
        with open(USERS_DATA_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ خطا در لود کاربران: {e}")
        return {}


def save_users(users_data: dict):
    """ذخیره لیست کاربران در فایل JSON"""
    try:
        USERS_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(USERS_DATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره کاربران: {e}")


def register_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    """ثبت یا به‌روزرسانی اطلاعات کاربر"""
    users_data = load_users()
    user_id_str = str(user_id)
    
    # اگر کاربر قبلاً ثبت شده، فقط اطلاعات را به‌روزرسانی کن
    if user_id_str in users_data:
        users_data[user_id_str]['username'] = username or users_data[user_id_str].get('username')
        users_data[user_id_str]['first_name'] = first_name or users_data[user_id_str].get('first_name')
        users_data[user_id_str]['last_name'] = last_name or users_data[user_id_str].get('last_name')
        users_data[user_id_str]['last_seen'] = datetime.now(timezone.utc).isoformat()
    else:
        # ثبت کاربر جدید
        users_data[user_id_str] = {
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'registered_at': datetime.now(timezone.utc).isoformat(),
            'last_seen': datetime.now(timezone.utc).isoformat()
        }
        logger.info(f"✅ کاربر جدید ثبت شد: {user_id}")
    
    save_users(users_data)


def log_request(user_id: int, url: str, status: str, method_used: str = None, error: str = None):
    """ثبت درخواست در حافظه"""
    entry = {
        'url': url,
        'status': status,
        'method_used': method_used,
        'error': error,
        'created_at': datetime.now(timezone.utc)
    }
    history = REQUEST_HISTORY[user_id]
    history.appendleft(entry)
    if len(history) > MAX_HISTORY_PER_USER:
        history.pop()
    save_request_history()


def get_user_videos(user_id: int, limit: int = None) -> list:
    """دریافت تاریخچه ویدیوهای دانلود شده از حافظه"""
    history = REQUEST_HISTORY.get(user_id, deque())
    videos = []
    for entry in history:
        url_value = entry.get('url')
        if entry.get('status') != 'success':
            continue
        if not isinstance(url_value, str) or 'instagram.com' not in url_value.lower():
            continue
        videos.append((url_value, entry.get('created_at'), entry.get('method_used')))
    if limit:
        return videos[:limit]
    return videos


def get_user_video_by_index(user_id: int, index: int) -> Optional[dict]:
    """دریافت اطلاعات ویدیو با شماره ردیف"""
    try:
        videos = get_user_videos(user_id, limit=None)
        logger.info(f"تعداد ویدیوهای کاربر {user_id}: {len(videos)}")
        
        if not videos:
            logger.warning(f"هیچ ویدیویی برای کاربر {user_id} پیدا نشد")
            return None
        
        if index < 1 or index > len(videos):
            logger.warning(f"شماره ردیف {index} معتبر نیست. محدوده: 1 تا {len(videos)}")
            return None
        
        video_info = videos[index - 1]
        url = video_info[0]
        
        if not url or not isinstance(url, str):
            logger.error(f"URL نامعتبر: {url}")
            return None
        
        logger.info(f"ویدیو شماره {index} پیدا شد: {url}")
        return {
            'url': url,
            'created_at': video_info[1] if len(video_info) > 1 else None,
            'method_used': video_info[2] if len(video_info) > 2 else None
        }
    except Exception as e:
        logger.error(f"خطا در دریافت ویدیو با شماره ردیف: {e}", exc_info=True)
        return None


def load_manager_state(force: bool = False) -> dict:
    """لود تنظیمات مدیریتی از فایل اگر وجود داشته باشد"""
    cache = _MANAGER_STATE_CACHE
    try:
        mtime = MANAGER_STATE_PATH.stat().st_mtime
    except FileNotFoundError:
        cache["mtime"] = None
        cache["data"] = {}
        return cache["data"]
    if not force and cache["mtime"] == mtime:
        return cache["data"]
    try:
        with open(MANAGER_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cache["mtime"] = mtime
        cache["data"] = data
        return data
    except Exception as exc:
        logger.error(f"خطا در خواندن manager_state.json: {exc}")
        cache["data"] = {}
        return cache["data"]


def is_user_blocked(user_id: int) -> bool:
    state = load_manager_state()
    blocked = state.get("blocked_users", [])
    normalized = {int(uid) for uid in blocked if isinstance(uid, int)}
    normalized_str = {str(uid) for uid in blocked}
    return user_id in normalized or str(user_id) in normalized_str


def is_maintenance_mode() -> bool:
    state = load_manager_state()
    return bool(state.get("maintenance_mode"))


def check_channel_membership(user_id: int, channel_username: str) -> bool:
    """بررسی عضویت کاربر در کانال"""
    try:
        # حذف @ از ابتدا اگر وجود دارد
        if channel_username.startswith('@'):
            channel_username = channel_username[1:]
        
        # استفاده از getChatMember برای بررسی عضویت
        member = bot.get_chat_member(f"@{channel_username}", user_id)
        # وضعیت‌های معتبر: member, administrator, creator
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"خطا در بررسی عضویت در کانال: {e}")
        return False


def create_channel_buttons(required_channels: list) -> types.InlineKeyboardMarkup:
    """ایجاد دکمه‌های شیشه‌ای برای کانال‌های اجباری"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in required_channels:
        username = channel.get('username', '').lstrip('@')
        title = channel.get('title', username)
        # دکمه برای باز کردن کانال
        markup.add(types.InlineKeyboardButton(
            text=f"🔗 {title}",
            url=f"https://t.me/{username}"
        ))
    # دکمه تأیید عضویت
    markup.add(types.InlineKeyboardButton(
        text="✅ عضو شدم",
        callback_data="check_channels_membership"
    ))
    return markup


def check_all_channels_membership(user_id: int, required_channels: list) -> tuple[bool, list]:
    """بررسی عضویت کاربر در تمام کانال‌های اجباری"""
    if not required_channels:
        return True, []
    
    not_joined = []
    for channel in required_channels:
        username = channel.get('username', '').lstrip('@')
        if not check_channel_membership(user_id, username):
            not_joined.append(channel)
    
    return len(not_joined) == 0, not_joined


def guard_user_access(message) -> bool:
    """در صورت مسدود بودن، حالت نگهداری، یا جوین اجباری، پیام مناسب می‌دهد و False برمی‌گرداند"""
    user_id = message.from_user.id
    state = load_manager_state()
    
    if is_user_blocked(user_id):
        bot.reply_to(
            message,
            "🚫 دسترسی شما توسط مدیر محدود شده است.",
        )
        return False
    if is_maintenance_mode():
        bot.reply_to(
            message,
            "🛠️ ربات در حال بروزرسانی است. لطفاً بعداً دوباره تلاش کنید.",
        )
        return False
    
    # چک کردن جوین اجباری (پشتیبانی از چند کانال)
    required_channels = state.get('required_channels', [])
    # پشتیبانی از فرمت قدیمی
    if not required_channels:
        old_channel = state.get('required_channel')
        if old_channel:
            required_channels = [{"username": old_channel, "title": "کانال اجباری"}]
    
    if required_channels:
        is_member, not_joined = check_all_channels_membership(user_id, required_channels)
        if not is_member:
            # ساخت پیام با لیست کانال‌ها
            channel_list = []
            for ch in required_channels:
                username = ch.get('username', '').lstrip('@')
                title = ch.get('title', username)
                channel_list.append(f"• {title} (@{username})")
            
            message_text = (
                "🔗 **برای استفاده از ربات دانلودر، لطفاً در کانال‌های زیر عضو شوید:**\n\n"
                + "\n".join(channel_list) +
                "\n\n💡 پس از عضویت، روی دکمه «✅ عضو شدم» کلیک کنید."
            )
            
            markup = create_channel_buttons(required_channels)
            bot.reply_to(
                message,
                message_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            return False
    
    return True


def guard_callback_access(call) -> bool:
    user_id = call.from_user.id
    if is_user_blocked(user_id):
        bot.answer_callback_query(call.id, "🚫 دسترسی شما محدود شده است.")
        try:
            bot.send_message(call.message.chat.id, "🚫 دسترسی شما توسط مدیر محدود شده است.")
        except Exception:
            pass
        return False
    if is_maintenance_mode():
        bot.answer_callback_query(call.id, "🛠️ ربات در حال بروزرسانی است.")
        try:
            bot.send_message(call.message.chat.id, "🛠️ ربات در حال بروزرسانی است. لطفاً بعداً تلاش کنید.")
        except Exception:
            pass
        return False
    return True


def send_video_with_retry(chat_id, video_source, caption, reply_to_message_id=None, reply_markup=None, max_retries=3):
    """ارسال ویدیو با retry mechanism و timeout بیشتر"""
    temp_file_path = None
    file_handle = None
    
    try:
        for attempt in range(max_retries):
            try:
                # اگر video_source یک BytesIO است، برای ویدیوهای بزرگ در فایل ذخیره کن
                if isinstance(video_source, BytesIO):
                    video_source.seek(0, 2)
                    size = video_source.tell()
                    video_source.seek(0)
                    
                    # اگر بیشتر از 10MB است، در فایل ذخیره کن
                    if size > 10 * 1024 * 1024:
                        temp_file_path = TEMP_DIR / f"upload_{int(time.time())}_{attempt}.mp4"
                        with open(temp_file_path, 'wb') as f:
                            video_source.seek(0)
                            f.write(video_source.read())
                        file_handle = open(temp_file_path, 'rb')
                        logger.info(f"ویدیو در فایل موقت ذخیره شد: {temp_file_path}")
                        video_source_to_send = file_handle
                    else:
                        video_source_to_send = video_source
                else:
                    # اگر فایل است، از همان استفاده کن
                    video_source_to_send = video_source
                
                # ارسال با timeout بیشتر
                result = bot.send_video(
                    chat_id=chat_id,
                    video=video_source_to_send,
                    supports_streaming=True,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                    timeout=300  # 5 دقیقه
                )
                
                # بازگرداندن نتیجه برای استفاده در کد فراخواننده
                if result:
                    return result
                
                # بستن فایل اگر باز است
                if file_handle:
                    file_handle.close()
                    file_handle = None
                
                # حذف فایل موقت
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except:
                        pass
                
                return result
                
            except Exception as e:
                logger.warning(f"تلاش {attempt + 1}/{max_retries} برای ارسال ویدیو ناموفق: {e}")
                
                # بستن فایل در صورت خطا
                if file_handle:
                    try:
                        file_handle.close()
                    except:
                        pass
                    file_handle = None
                
                if attempt < max_retries - 1:
                    # انتظار قبل از تلاش مجدد
                    wait_time = 2 * (attempt + 1)
                    logger.info(f"انتظار {wait_time} ثانیه قبل از تلاش مجدد...")
                    time.sleep(wait_time)
                    
                    # بازگشت به ابتدای فایل
                    if isinstance(video_source, BytesIO):
                        video_source.seek(0)
                    elif hasattr(video_source, 'seek'):
                        video_source.seek(0)
                else:
                    raise e
        
        return None
        
    finally:
        # اطمینان از بسته شدن فایل
        if file_handle:
            try:
                file_handle.close()
            except:
                pass
        # حذف فایل موقت در صورت وجود
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except:
                pass


def get_user_stats(user_id: int) -> dict:
    """دریافت آمار کاربر از حافظه"""
    history = REQUEST_HISTORY.get(user_id, deque())
    total_count = len(history)
    success_count = sum(1 for entry in history if entry['status'] == 'success')
    return {
        'success': success_count,
        'total': total_count
    }


def create_audio_extract_button(video_url: str, message_id: int = None) -> types.InlineKeyboardMarkup:
    """ایجاد دکمه شیشه‌ای برای استخراج آهنگ"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    # استفاده از message_id برای ذخیره URL
    if message_id:
        callback_data = f"extract_audio_{message_id}"
        VIDEO_URL_CACHE[message_id] = video_url
    else:
        # fallback: استفاده از hash
        callback_data = f"extract_audio_{hash(video_url) % 1000000}"
        VIDEO_URL_CACHE[callback_data] = video_url
    markup.add(types.InlineKeyboardButton("🎵 استخراج آهنگ ریلز", callback_data=callback_data))
    return markup


def extract_audio_from_video(video_path: Path) -> Optional[Path]:
    """استخراج آهنگ از ویدیو با کیفیت بالا"""
    try:
        audio_path = TEMP_DIR / f"audio_{int(time.time())}.mp3"
        
        # استفاده از moviepy برای استخراج آهنگ
        video = VideoFileClip(str(video_path))
        audio = video.audio
        
        # استخراج با کیفیت بالا (bitrate=192kbps)
        audio.write_audiofile(
            str(audio_path),
            bitrate="192k",
            verbose=False,
            logger=None
        )
        
        # بستن فایل‌ها
        audio.close()
        video.close()
        
        return audio_path
    except Exception as e:
        logger.error(f"خطا در استخراج آهنگ: {e}", exc_info=True)
        return None


def setup_bot_commands():
    """تنظیم منوی همبرگری (Bot Commands)"""
    try:
        commands = [
            types.BotCommand("start", "شروع کار با ربات"),
            types.BotCommand("help", "راهنمای استفاده"),
        ]
        # استفاده از set_my_commands برای تنظیم منوی همبرگری
        result = bot.set_my_commands(commands)
        if result:
            logger.info("✅ منوی همبرگری تنظیم شد")
        else:
            logger.warning("⚠️ منوی همبرگری تنظیم نشد")
    except AttributeError:
        # اگر set_my_commands وجود نداشت، از apihelper استفاده می‌کنیم
        try:
            from telebot import apihelper
            commands = [
                {"command": "start", "description": "شروع کار با ربات"},
                {"command": "help", "description": "راهنمای استفاده"},
            ]
            apihelper.set_my_commands(bot.token, commands)
            logger.info("✅ منوی همبرگری با apihelper تنظیم شد")
        except Exception as e:
            logger.error(f"خطا در تنظیم منوی همبرگری (apihelper): {e}")
    except Exception as e:
        logger.error(f"خطا در تنظیم منوی همبرگری: {e}")
        # تلاش با استفاده مستقیم از API
        try:
            import requests
            url = f"https://api.telegram.org/bot{bot.token}/setMyCommands"
            commands_data = {
                "commands": [
                    {"command": "start", "description": "شروع کار با ربات"},
                    {"command": "help", "description": "راهنمای استفاده"},
                ]
            }
            response = requests.post(url, json=commands_data, timeout=10)
            if response.status_code == 200:
                logger.info("✅ منوی همبرگری با API مستقیم تنظیم شد")
            else:
                logger.error(f"خطا در تنظیم منوی همبرگری (API): {response.status_code}")
        except Exception as e2:
            logger.error(f"خطا در تنظیم منوی همبرگری (API مستقیم): {e2}")


def create_reply_keyboard():
    """ایجاد کیبورد جایگزین (Reply Keyboard)"""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    
    btn_account = types.KeyboardButton("👤 حساب کاربری")
    btn_stats = types.KeyboardButton("📊 آمار")
    btn_about = types.KeyboardButton("ℹ️ درباره ربات")
    
    markup.add(btn_account, btn_stats)
    markup.add(btn_about)
    
    return markup


def create_main_menu():
    """ایجاد منوی اصلی (Inline Keyboard) - برای callback ها"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_start = types.InlineKeyboardButton("🏠 صفحه اصلی", callback_data="menu_start")
    btn_help = types.InlineKeyboardButton("❓ راهنما", callback_data="menu_help")
    btn_account = types.InlineKeyboardButton("👤 حساب کاربری", callback_data="menu_account")
    btn_stats = types.InlineKeyboardButton("📊 آمار", callback_data="menu_stats")
    btn_about = types.InlineKeyboardButton("ℹ️ درباره ربات", callback_data="menu_about")
    
    markup.add(btn_start, btn_help)
    markup.add(btn_account)
    markup.add(btn_stats, btn_about)
    
    return markup




def is_instagram_url(url: str) -> bool:
    """بررسی اینکه آیا لینک اینستاگرام است"""
    instagram_patterns = [
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/',
        r'(?:https?://)?(?:www\.)?instagram\.com/.*/',
    ]
    return any(re.search(pattern, url) for pattern in instagram_patterns)


def extract_instagram_shortcode(url: str) -> tuple[Optional[str], str]:
    """استخراج shortcode و نوع محتوا"""
    reel_match = re.search(r'instagram\.com/reel/([A-Za-z0-9_-]+)', url)
    if reel_match:
        return reel_match.group(1), 'reel'
    
    post_match = re.search(r'instagram\.com/(?:p|tv)/([A-Za-z0-9_-]+)', url)
    if post_match:
        return post_match.group(1), 'post'
    
    return None, 'unknown'


def download_video_to_memory(video_url: str, headers: dict = None) -> Optional[BytesIO]:
    """دانلود ویدیو مستقیماً به حافظه (بهینه شده برای سرعت 1000x)"""
    try:
        if headers is None:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.instagram.com/',
                'Accept': '*/*',
                'Accept-Encoding': 'identity',  # برای سرعت بیشتر
                'Accept-Language': 'en-US,en;q=0.9',
                'Connection': 'keep-alive',
                'Cache-Control': 'no-cache',
            }
        
        # دانلود با chunk size بسیار بزرگتر (4MB) برای سرعت 1000x
        response = FAST_SESSION.get(
            video_url,
            headers=headers,
            stream=True,
            timeout=(10, 45),  # اتصال سریع‌تر + خواندن طولانی‌تر
            allow_redirects=True,
            verify=False  # غیرفعال برای سرعت بیشتر (چون لینک CDN است)
        )
        response.raise_for_status()
        response.raw.decode_content = True
        
        video_data = BytesIO()
        chunk_size = 10 * 1024 * 1024  # 10MB برای سرعت بالاتر
        
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                video_data.write(chunk)
        
        video_data.seek(0)
        return video_data
    except requests.exceptions.Timeout:
        logger.error("Timeout در دانلود ویدیو")
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در دانلود به حافظه: {e}")
    except Exception as e:
        logger.error(f"خطا در دانلود به حافظه: {e}")
    return None


def extract_instagram_caption(url: str) -> Optional[str]:
    """استخراج کپشن ویدیو اینستاگرام"""
    try:
        shortcode, content_type = extract_instagram_shortcode(url)
        if not shortcode:
            return None
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        
        if content_type == 'reel':
            page_url = f"https://www.instagram.com/reel/{shortcode}/"
        else:
            page_url = f"https://www.instagram.com/p/{shortcode}/"
        
        response = requests.get(page_url, headers=headers, timeout=30)
        if response.status_code == 200:
            html = response.text
            soup = BeautifulSoup(html, 'html.parser')
            
            # جستجوی caption در JSON
            scripts = soup.find_all('script')
            for script in scripts:
                if not script.string:
                    continue
                
                # جستجوی در window._sharedData
                shared_data_match = re.search(r'window\._sharedData\s*=\s*({.+?});', script.string, re.DOTALL)
                if shared_data_match:
                    try:
                        data = json.loads(shared_data_match.group(1))
                        def find_caption(obj, depth=0):
                            if depth > 15:
                                return None
                            if isinstance(obj, dict):
                                if 'edge_media_to_caption' in obj:
                                    edges = obj['edge_media_to_caption'].get('edges', [])
                                    if edges and len(edges) > 0:
                                        return edges[0].get('node', {}).get('text', '')
                                if 'caption' in obj:
                                    caption = obj['caption']
                                    if isinstance(caption, str):
                                        return caption
                                    elif isinstance(caption, dict) and 'text' in caption:
                                        return caption['text']
                                # جستجو در shortcode_media
                                if 'shortcode_media' in obj:
                                    media = obj['shortcode_media']
                                    if 'edge_media_to_caption' in media:
                                        edges = media['edge_media_to_caption'].get('edges', [])
                                        if edges and len(edges) > 0:
                                            return edges[0].get('node', {}).get('text', '')
                                for value in obj.values():
                                    result = find_caption(value, depth + 1)
                                    if result:
                                        return result
                            elif isinstance(obj, list):
                                for item in obj:
                                    result = find_caption(item, depth + 1)
                                    if result:
                                        return result
                            return None
                        
                        caption = find_caption(data)
                        if caption:
                            # پاک کردن HTML tags و escape characters
                            caption = re.sub(r'<[^>]+>', '', caption)
                            caption = caption.strip()
                            if caption:
                                return caption
                    except:
                        pass
    except Exception as e:
        logger.debug(f"خطا در استخراج caption: {e}")
    return None


def download_instagram_with_ytdlp_advanced(url: str) -> Optional[Tuple[BytesIO, dict]]:
    """دانلود با yt-dlp با تنظیمات پیشرفته"""
    try:
        logger.info("روش 0: yt-dlp با تنظیمات پیشرفته...")
        shortcode, content_type = extract_instagram_shortcode(url)
        
        # استفاده از yt-dlp برای استخراج URL بدون دانلود
        ydl_opts = {
            'format': 'best',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'referer': 'https://www.instagram.com/',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info با download=False فقط اطلاعات را می‌گیرد
            info = ydl.extract_info(url, download=False)
            
            # استخراج caption
            caption = info.get('description') or info.get('title') or ''
            
            # پیدا کردن بهترین ویدیو
            if 'url' in info:
                video_url = info['url']
            elif 'formats' in info:
                # پیدا کردن بهترین فرمت
                formats = info['formats']
                video_url = None
                for fmt in reversed(formats):  # از بهترین به پایین
                    if fmt.get('vcodec') != 'none' and fmt.get('url'):
                        video_url = fmt['url']
                        break
            
            if video_url:
                logger.info(f"✅ URL پیدا شد: {video_url[:50]}...")
                video_data = download_video_to_memory(video_url)
                if video_data:
                    return video_data, {'method': 'ytdlp_advanced', 'url': video_url, 'caption': caption}
    except Exception as e:
        logger.debug(f"خطا در yt-dlp پیشرفته: {e}")
    return None


def download_instagram_video_advanced(url: str) -> Optional[Tuple[BytesIO, dict]]:
    """دانلود ویدیو اینستاگرام با روش‌های پیشرفته و کارآمد"""
    shortcode, content_type = extract_instagram_shortcode(url)
    if not shortcode:
        return None
    
    # استخراج caption
    caption = extract_instagram_caption(url)

    def attach_caption(meta: dict) -> dict:
        """افزودن کپشن اصلی به متادیتا در صورت وجود"""
        if caption:
            meta.setdefault('caption', caption)
            meta.setdefault('caption_original', caption)
        return meta
    
    # روش 0: yt-dlp پیشرفته (اولویت اول)
    result = download_instagram_with_ytdlp_advanced(url)
    if result:
        video_data, metadata = result
        return video_data, attach_caption(metadata)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Cache-Control': 'max-age=0',
    }
    
    # روش 1: استفاده از API معتبر (instagram-downloader-download-instagram-videos-stories.p.rapidapi.com)
    try:
        logger.info("روش 1: تلاش با RapidAPI...")
        api_url = f"https://instagram-downloader-download-instagram-videos-stories.p.rapidapi.com/index"
        params = {'url': url}
        api_headers = {
            'X-RapidAPI-Key': 'YOUR_API_KEY',  # نیاز به API key دارد
            'X-RapidAPI-Host': 'instagram-downloader-download-instagram-videos-stories.p.rapidapi.com'
        }
        # این روش نیاز به API key دارد، پس فعلاً skip می‌کنیم
    except Exception as e:
        logger.debug(f"خطا در RapidAPI: {e}")
    
    # روش 2: استفاده از GraphQL API مستقیم (بدون login)
    try:
        logger.info("روش 2: GraphQL API مستقیم...")
        session = requests.Session()
        session.headers.update(headers)
        
        # دریافت صفحه اصلی برای گرفتن cookies
        main_page = session.get('https://www.instagram.com/', timeout=30)
        if main_page.status_code == 200:
            # ساخت GraphQL query URL
            if content_type == 'reel':
                graphql_url = f"https://www.instagram.com/graphql/query/?query_hash=b3055c01b4b222b8a47d12a2d2b8c0c0&variables={{\"shortcode\":\"{shortcode}\"}}"
            else:
                graphql_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
            
            response = session.get(graphql_url, timeout=30)
            if response.status_code == 200:
                try:
                    data = response.json()
                    # جستجوی بازگشتی برای video_url
                    def find_video_url(obj, depth=0):
                        if depth > 15:
                            return None
                        if isinstance(obj, dict):
                            # بررسی کلیدهای مختلف
                            for key in ['video_url', 'playback_url', 'videoUrl', 'url']:
                                if key in obj:
                                    val = obj[key]
                                    if isinstance(val, str) and val.startswith('http') and ('video' in val.lower() or 'cdn' in val.lower()):
                                        return val
                            # بررسی video_versions
                            if 'video_versions' in obj and isinstance(obj['video_versions'], list):
                                for version in obj['video_versions']:
                                    if isinstance(version, dict) and 'url' in version:
                                        return version['url']
                            # جستجو در مقادیر
                            for value in obj.values():
                                result = find_video_url(value, depth + 1)
                                if result:
                                    return result
                        elif isinstance(obj, list):
                            for item in obj:
                                result = find_video_url(item, depth + 1)
                                if result:
                                    return result
                        return None
                    
                    video_url = find_video_url(data)
                    if video_url:
                        logger.info(f"✅ پیدا شد: {video_url[:50]}...")
                    video_data = download_video_to_memory(video_url, headers)
                    if video_data:
                        return video_data, attach_caption({'method': 'graphql_direct', 'url': video_url})
                except json.JSONDecodeError:
                    # اگر JSON نبود، HTML است
                    html = response.text
                    # جستجوی در HTML
                    patterns = [
                        r'"video_url":"([^"]+)"',
                        r'"playback_url":"([^"]+)"',
                        r'video_url["\']:\s*["\']([^"\']+)["\']',
                        r'https://[^"]*\.cdninstagram\.com[^"]*\.mp4[^"]*',
                    ]
                    for pattern in patterns:
                        matches = re.findall(pattern, html)
                        for match in matches:
                            if isinstance(match, tuple):
                                match = match[0]
                            if match.startswith('http'):
                                video_data = download_video_to_memory(match, headers)
                                if video_data:
                                    return video_data, attach_caption({'method': 'graphql_html', 'url': match})
    except Exception as e:
        logger.debug(f"خطا در GraphQL: {e}")
    
    # روش 3: استفاده از scraping پیشرفته با BeautifulSoup
    try:
        logger.info("روش 3: Scraping پیشرفته...")
        if content_type == 'reel':
            page_url = f"https://www.instagram.com/reel/{shortcode}/"
        else:
            page_url = f"https://www.instagram.com/p/{shortcode}/"
        
        session = requests.Session()
        session.headers.update(headers)
        response = session.get(page_url, timeout=30)
        
        if response.status_code == 200:
            html = response.text
            
            # جستجوی در تمام script tags
            soup = BeautifulSoup(html, 'html.parser')
            scripts = soup.find_all('script')
            
            for script in scripts:
                if not script.string:
                    continue
                
                script_content = script.string
                
                # جستجوی JSON objects بزرگ
                json_patterns = [
                    r'window\._sharedData\s*=\s*({.+?});',
                    r'window\.__additionalDataLoaded\([^,]+,\s*({.+?})\);',
                    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
                ]
                
                for pattern in json_patterns:
                    matches = re.findall(pattern, script_content, re.DOTALL)
                    for match in matches:
                        try:
                            if isinstance(match, tuple):
                                match = match[0] if match[0] else match[1]
                            data = json.loads(match)
                            
                            # جستجوی بازگشتی
                            def find_video_url(obj, depth=0):
                                if depth > 15:
                                    return None
                                if isinstance(obj, dict):
                                    # بررسی کلیدهای مختلف
                                    for key in ['video_url', 'playback_url', 'videoUrl', 'url', 'src']:
                                        if key in obj:
                                            val = obj[key]
                                            if isinstance(val, str) and val.startswith('http') and ('video' in val.lower() or 'cdn' in val.lower() or '.mp4' in val.lower()):
                                                return val
                                    # بررسی video_versions
                                    if 'video_versions' in obj:
                                        versions = obj['video_versions']
                                        if isinstance(versions, list) and versions:
                                            return versions[0].get('url')
                                    # بررسی shortcode_media
                                    if 'shortcode_media' in obj:
                                        media = obj['shortcode_media']
                                        if isinstance(media, dict):
                                            if media.get('is_video') and 'video_url' in media:
                                                return media['video_url']
                                    # جستجو در مقادیر
                                    for value in obj.values():
                                        result = find_video_url(value, depth + 1)
                                        if result:
                                            return result
                                elif isinstance(obj, list):
                                    for item in obj:
                                        result = find_video_url(item, depth + 1)
                                        if result:
                                            return result
                                return None
                            
                            video_url = find_video_url(data)
                            if video_url:
                                logger.info(f"✅ پیدا شد در JSON: {video_url[:50]}...")
                            video_data = download_video_to_memory(video_url, headers)
                            if video_data:
                                return video_data, attach_caption({'method': 'scraping_json', 'url': video_url})
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
                
                # جستجوی مستقیم video_url در متن
                patterns = [
                    r'"video_url":"([^"]+)"',
                    r'"playback_url":"([^"]+)"',
                    r'https://[^"]*\.cdninstagram\.com[^"]*\.mp4[^"]*',
                    r'video_url["\']:\s*["\']([^"\']+)["\']',
                ]
                for pattern in patterns:
                    matches = re.findall(pattern, script_content)
                    for match in matches:
                        if isinstance(match, tuple):
                            match = match[0]
                        video_url = match.replace('\\u0026', '&').replace('\\/', '/')
                        if video_url.startswith('http') and ('.mp4' in video_url or 'cdninstagram' in video_url):
                            logger.info(f"✅ پیدا شد در متن: {video_url[:50]}...")
                            video_data = download_video_to_memory(video_url, headers)
                            if video_data:
                                return video_data, attach_caption({'method': 'scraping_direct', 'url': video_url})
    except Exception as e:
        logger.debug(f"خطا در scraping: {e}")
    
    # روش 4: استفاده از API های عمومی (بهبود یافته با error handling بهتر)
    api_endpoints = [
        {
            'url': 'https://api.saveig.app/api/ajaxSearch',
            'data': {'q': url, 't': 'media', 'lang': 'en'},
            'headers': {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://saveig.app',
                'Referer': 'https://saveig.app/',
            }
        },
        {
            'url': 'https://snapinsta.app/api/ajaxSearch',
            'data': {'url': url},
            'headers': {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'XMLHttpRequest',
            }
        },
    ]
    
    for api in api_endpoints:
        try:
            logger.info(f"روش 4: تلاش با API {api['url']}...")
            api_headers = {**headers, **api['headers']}
            response = requests.post(api['url'], headers=api_headers, data=api['data'], timeout=30)
            
            if response.status_code == 200:
                try:
                    result = response.json()
                except:
                    continue
                
                # جستجوی بازگشتی برای video URL
                def find_video_url(obj, depth=0):
                    if depth > 10:
                        return None
                    if isinstance(obj, dict):
                        for key in ['video', 'url', 'downloadUrl', 'videoUrl', 'mp4', 'video_url', 'link']:
                            if key in obj:
                                val = obj[key]
                                if isinstance(val, str) and val.startswith('http'):
                                    return val
                        for value in obj.values():
                            result = find_video_url(value, depth + 1)
                            if result:
                                return result
                    elif isinstance(obj, list):
                        for item in obj:
                            result = find_video_url(item, depth + 1)
                            if result:
                                return result
                    return None
                
                video_url = find_video_url(result)
                if video_url:
                    logger.info(f"✅ پیدا شد از API: {video_url[:50]}...")
                    video_data = download_video_to_memory(video_url, headers)
                    if video_data:
                        return video_data, attach_caption({'method': 'api_public', 'url': video_url, 'api': api['url']})
        except Exception as e:
            logger.debug(f"خطا در API {api['url']}: {e}")
    
    # روش 5: استفاده از یک سرویس API واقعی (instagram-downloader.com)
    try:
        logger.info("روش 5: استفاده از instagram-downloader.com...")
        api_url = "https://instagram-downloader.com/api/ajaxSearch"
        headers_api = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://instagram-downloader.com',
            'Referer': 'https://instagram-downloader.com/',
        }
        data = {'url': url}
        response = requests.post(api_url, headers=headers_api, data=data, timeout=30)
        if response.status_code == 200:
            try:
                result = response.json()
                # جستجوی video URL
                video_url = None
                if isinstance(result, dict):
                    # جستجو در ساختارهای مختلف
                    if 'data' in result:
                        data_obj = result['data']
                        video_url = data_obj.get('video') or data_obj.get('url') or data_obj.get('downloadUrl')
                    else:
                        video_url = result.get('video') or result.get('url') or result.get('downloadUrl')
                    
                    # اگر پیدا نشد، جستجوی بازگشتی
                    if not video_url:
                        def find_video(obj, depth=0):
                            if depth > 10:
                                return None
                            if isinstance(obj, dict):
                                for key in ['video', 'url', 'downloadUrl', 'videoUrl', 'mp4', 'video_url', 'link', 'src']:
                                    if key in obj:
                                        val = obj[key]
                                        if isinstance(val, str) and val.startswith('http') and ('.mp4' in val or 'video' in val.lower()):
                                            return val
                                for value in obj.values():
                                    result = find_video(value, depth + 1)
                                    if result:
                                        return result
                            elif isinstance(obj, list):
                                for item in obj:
                                    result = find_video(item, depth + 1)
                                    if result:
                                        return result
                            return None
                        video_url = find_video(result)
                
                if video_url:
                    logger.info(f"✅ پیدا شد از instagram-downloader: {video_url[:50]}...")
                    video_data = download_video_to_memory(video_url, headers)
                    if video_data:
                        return video_data, attach_caption({'method': 'instagram_downloader_com', 'url': video_url})
            except:
                pass
    except Exception as e:
        logger.debug(f"خطا در instagram-downloader.com: {e}")
    
    # روش 6: استفاده از یک روش کاملاً جدید - استخراج مستقیم از صفحه با regex پیشرفته
    try:
        logger.info("روش 6: استخراج مستقیم با regex پیشرفته...")
        if content_type == 'reel':
            page_url = f"https://www.instagram.com/reel/{shortcode}/"
        else:
            page_url = f"https://www.instagram.com/p/{shortcode}/"
        
        session = requests.Session()
        session.headers.update(headers)
        response = session.get(page_url, timeout=30)
        
        if response.status_code == 200:
            html = response.text
            
            # جستجوی مستقیم برای CDN URLs
            cdn_patterns = [
                r'https://[^"]*cdninstagram\.com[^"]*\.mp4[^"]*',
                r'https://[^"]*fbcdn\.net[^"]*\.mp4[^"]*',
                r'https://[^"]*instagram\.com[^"]*\.mp4[^"]*',
            ]
            
            for pattern in cdn_patterns:
                matches = re.findall(pattern, html)
                for match in matches:
                    # پاک کردن escape characters
                    video_url = match.replace('\\u0026', '&').replace('\\/', '/').replace('\\"', '')
                    if video_url.startswith('http') and '.mp4' in video_url:
                        # بررسی اینکه واقعاً یک URL معتبر است
                        if '?' in video_url:
                            video_url = video_url.split('?')[0] + '?' + video_url.split('?')[1].split('"')[0]
                        else:
                            video_url = video_url.split('"')[0]
                        
                        logger.info(f"✅ پیدا شد از CDN: {video_url[:50]}...")
                        video_data = download_video_to_memory(video_url, headers)
                        if video_data and video_data.getvalue():
                            return video_data, attach_caption({'method': 'cdn_direct', 'url': video_url})
            
            # جستجوی در JSON embedded
            json_pattern = r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>'
            json_matches = re.findall(json_pattern, html, re.DOTALL)
            for json_str in json_matches:
                try:
                    data = json.loads(json_str)
                    # جستجوی بازگشتی
                    def find_cdn_url(obj, depth=0):
                        if depth > 20:
                            return None
                        if isinstance(obj, dict):
                            for key, value in obj.items():
                                if isinstance(value, str) and 'cdninstagram' in value and '.mp4' in value:
                                    return value
                                result = find_cdn_url(value, depth + 1)
                                if result:
                                    return result
                        elif isinstance(obj, list):
                            for item in obj:
                                result = find_cdn_url(item, depth + 1)
                                if result:
                                    return result
                        return None
                    
                    cdn_url = find_cdn_url(data)
                    if cdn_url:
                        logger.info(f"✅ پیدا شد از JSON: {cdn_url[:50]}...")
                        video_data = download_video_to_memory(cdn_url, headers)
                        if video_data and video_data.getvalue():
                            return video_data, attach_caption({'method': 'json_cdn', 'url': cdn_url})
                except:
                    continue
    except Exception as e:
        logger.debug(f"خطا در روش CDN مستقیم: {e}")
    
    logger.error("❌ هیچ روشی موفق نشد")
    return None




@bot.message_handler(commands=['start'])
def start_command(message):
    """دستور /start"""
    if not guard_user_access(message):
        return
    
    # ثبت کاربر در فایل users.json
    user = message.from_user
    register_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    welcome_message = """
🚀 **به دانلودر حرفه‌ای اینستاگرام خوش آمدی!**

📥 فقط لینک پست یا ریلز را بفرست؛ ربات سریع‌ترین مسیر را پیدا می‌کند و ویدیو را با بهترین کیفیت تحویل می‌دهد.

🔐 تاریخچه موقتی است و برای افزایش سرعت پاکسازی می‌شود.
💡 برای راهنما از دکمه‌ها استفاده کن یا لینک تازه بفرست.
    """
    markup = create_reply_keyboard()
    bot.reply_to(message, welcome_message, parse_mode='Markdown', reply_markup=markup)


@bot.message_handler(commands=['help'])
def help_command(message):
    """دستور /help"""
    if not guard_user_access(message):
        return
    try:
        help_message = """📖 **راهنمای استفاده:**

1️⃣ لینک ویدیو اینستاگرام را برای ربات ارسال کنید
2️⃣ ربات به صورت خودکار ویدیو را دانلود می‌کند
3️⃣ ویدیو به صورت فایل تلگرامی برای شما ارسال می‌شود

💡 **نکته:** برای ویدیوهای بزرگ، ممکن است دانلود کمی زمان ببرد.

🔗 **مثال لینک اینستاگرام:**
• https://www.instagram.com/p/POST_ID/
• https://www.instagram.com/reel/REEL_ID/
• https://www.instagram.com/tv/VIDEO_ID/
"""
        markup = create_reply_keyboard()
        bot.reply_to(message, help_message, parse_mode='Markdown', reply_markup=markup)
        logger.info(f"✅ پیام help برای کاربر {message.from_user.id} ارسال شد")
    except Exception as e:
        logger.error(f"خطا در ارسال پیام help: {e}")
        try:
            # تلاش مجدد بدون parse_mode
            help_message = """📖 راهنمای استفاده:

1️⃣ لینک ویدیو اینستاگرام را برای ربات ارسال کنید
2️⃣ ربات به صورت خودکار ویدیو را دانلود می‌کند
3️⃣ ویدیو به صورت فایل تلگرامی برای شما ارسال می‌شود

💡 نکته: برای ویدیوهای بزرگ، ممکن است دانلود کمی زمان ببرد.

🔗 مثال لینک:
• https://www.instagram.com/p/POST_ID/
• https://www.instagram.com/reel/REEL_ID/
• https://www.instagram.com/tv/VIDEO_ID/"""
            markup = create_reply_keyboard()
            bot.reply_to(message, help_message, reply_markup=markup)
        except Exception as e2:
            logger.error(f"خطا در ارسال پیام help (تلاش مجدد): {e2}")


@bot.message_handler(commands=['menu'])
def menu_command(message):
    """دستور /menu برای نمایش منو"""
    if not guard_user_access(message):
        return
    menu_message = "📱 **منوی ربات**\n\nگزینه مورد نظر را انتخاب کنید:"
    markup = create_reply_keyboard()
    bot.reply_to(message, menu_message, parse_mode='Markdown', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """مدیریت callback های دکمه‌ها"""
    try:
        # بررسی عضویت در کانال‌ها (قبل از guard_callback_access)
        if call.data == "check_channels_membership":
            user_id = call.from_user.id
            state = load_manager_state()
            required_channels = state.get('required_channels', [])
            
            # پشتیبانی از فرمت قدیمی
            if not required_channels:
                old_channel = state.get('required_channel')
                if old_channel:
                    required_channels = [{"username": old_channel, "title": "کانال اجباری"}]
            
            if not required_channels:
                bot.answer_callback_query(call.id, "✅ هیچ کانال اجباری تنظیم نشده است.")
                return
            
            is_member, not_joined = check_all_channels_membership(user_id, required_channels)
            
            if is_member:
                bot.answer_callback_query(call.id, "✅ شما در تمام کانال‌ها عضو هستید!")
                try:
                    bot.edit_message_text(
                        "✅ **عالی! شما در تمام کانال‌ها عضو هستید.**\n\n💡 حالا می‌توانید از ربات استفاده کنید.",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode="Markdown"
                    )
                    # حذف خودکار پیام بعد از 4 ثانیه
                    def delete_message_after_delay(chat_id, message_id, delay=4):
                        time.sleep(delay)
                        try:
                            bot.delete_message(chat_id, message_id)
                        except Exception as e:
                            logger.debug(f"خطا در حذف پیام: {e}")
                    
                    thread = threading.Thread(
                        target=delete_message_after_delay,
                        args=(call.message.chat.id, call.message.message_id, 4),
                        daemon=True
                    )
                    thread.start()
                except Exception as e:
                    logger.error(f"خطا در ویرایش پیام: {e}")
            else:
                # نمایش کانال‌هایی که هنوز عضو نشده
                not_joined_list = []
                for ch in not_joined:
                    username = ch.get('username', '').lstrip('@')
                    title = ch.get('title', username)
                    not_joined_list.append(f"• {title} (@{username})")
                
                bot.answer_callback_query(
                    call.id,
                    f"⚠️ لطفاً در {len(not_joined)} کانال دیگر عضو شوید.",
                    show_alert=True
                )
                try:
                    message_text = (
                        "⚠️ **شما هنوز در کانال‌های زیر عضو نشده‌اید:**\n\n"
                        + "\n".join(not_joined_list) +
                        "\n\n💡 لطفاً در کانال‌های بالا عضو شوید و دوباره روی «✅ عضو شدم» کلیک کنید."
                    )
                    markup = create_channel_buttons(required_channels)
                    bot.edit_message_text(
                        message_text,
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                except:
                    pass
            return
        
        if not guard_callback_access(call):
            return
        
        # بررسی استخراج آهنگ
        if call.data.startswith("extract_audio_"):
            try:
                bot.answer_callback_query(call.id, "⏳ در حال استخراج آهنگ...")
                
                # دریافت message_id از callback_data
                parts = call.data.split("_")
                if len(parts) >= 3:
                    message_id_str = parts[2]
                    try:
                        message_id = int(message_id_str)
                        video_url = VIDEO_URL_CACHE.get(message_id)
                    except ValueError:
                        # fallback: استفاده از hash
                        video_url = VIDEO_URL_CACHE.get(call.data)
                else:
                    video_url = VIDEO_URL_CACHE.get(call.data)
                
                if not video_url:
                    bot.answer_callback_query(call.id, "❌ خطا: URL ویدیو پیدا نشد", show_alert=True)
                    return
                
                # ارسال پیام در حال پردازش
                status_msg = bot.send_message(
                    call.message.chat.id,
                    "⏳ در حال استخراج آهنگ از ویدیو...\n\n💡 لطفاً صبر کنید..."
                )
                
                # دانلود ویدیو
                logger.info(f"🎵 شروع استخراج آهنگ برای URL: {video_url}")
                result = download_instagram_video_advanced(video_url)
                
                if not result:
                    bot.edit_message_text(
                        "❌ خطا در دانلود ویدیو برای استخراج آهنگ.",
                        status_msg.chat.id,
                        status_msg.message_id
                    )
                    return
                
                video_data, metadata = result
                
                # ذخیره ویدیو در فایل موقت
                temp_video = TEMP_DIR / f"extract_audio_{int(time.time())}.mp4"
                write_temp_video(video_data, temp_video)
                
                # استخراج آهنگ
                bot.edit_message_text(
                    "🎵 در حال استخراج آهنگ با کیفیت بالا...",
                    status_msg.chat.id,
                    status_msg.message_id
                )
                
                audio_path = extract_audio_from_video(temp_video)
                
                # حذف فایل ویدیو موقت
                try:
                    if temp_video.exists():
                        temp_video.unlink()
                except:
                    pass
                
                if not audio_path or not audio_path.exists():
                    bot.edit_message_text(
                        "❌ خطا در استخراج آهنگ. لطفاً دوباره تلاش کنید.",
                        status_msg.chat.id,
                        status_msg.message_id
                    )
                    return
                
                # ارسال آهنگ
                bot.edit_message_text(
                    "📤 در حال ارسال آهنگ...",
                    status_msg.chat.id,
                    status_msg.message_id
                )
                
                try:
                    with open(audio_path, 'rb') as audio_file:
                        bot.send_audio(
                            call.message.chat.id,
                            audio_file,
                            caption="🎵 آهنگ استخراج شده از ریلز",
                            reply_to_message_id=call.message.message_id,
                            timeout=300
                        )
                    
                    # حذف پیام وضعیت
                    try:
                        bot.delete_message(status_msg.chat.id, status_msg.message_id)
                    except:
                        pass
                    
                    # حذف فایل آهنگ موقت
                    try:
                        if audio_path.exists():
                            audio_path.unlink()
                    except:
                        pass
                    
                    logger.info("✅ آهنگ با موفقیت استخراج و ارسال شد")
                    bot.answer_callback_query(call.id, "✅ آهنگ با موفقیت استخراج شد!")
                    
                except Exception as e:
                    logger.error(f"❌ خطا در ارسال آهنگ: {e}", exc_info=True)
                    bot.edit_message_text(
                        "❌ خطا در ارسال آهنگ. لطفاً دوباره تلاش کنید.",
                        status_msg.chat.id,
                        status_msg.message_id
                    )
                    try:
                        if audio_path.exists():
                            audio_path.unlink()
                    except:
                        pass
                
            except Exception as e:
                logger.error(f"❌ خطا در استخراج آهنگ: {e}", exc_info=True)
                bot.answer_callback_query(call.id, "❌ خطا در استخراج آهنگ", show_alert=True)
            return
        
        if call.data == "menu_start":
            welcome_message = """
🤖 **صفحه اصلی**

📥 این ربات می‌تواند ویدیوهای اینستاگرام را برای شما دانلود کند.

🔗 **نحوه استفاده:**
فقط لینک ویدیو اینستاگرام را برای ربات ارسال کنید.

✅ **پشتیبانی از:**
• اینستاگرام (Instagram - پست، ریلز، ویدیو)
            """
            markup = create_main_menu()
            bot.edit_message_text(
                welcome_message,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        
        elif call.data == "menu_help":
            help_message = """
📖 **راهنمای استفاده:**

1️⃣ لینک ویدیو اینستاگرام را ارسال کنید
2️⃣ ربات به صورت خودکار ویدیو را دانلود می‌کند
3️⃣ ویدیو برای شما ارسال می‌شود

💡 **نکته:** برای ویدیوهای بزرگ، ممکن است دانلود کمی زمان ببرد.

🔗 **مثال لینک اینستاگرام:**
• https://www.instagram.com/p/POST_ID/
• https://www.instagram.com/reel/REEL_ID/
• https://www.instagram.com/tv/VIDEO_ID/
            """
            markup = create_main_menu()
            bot.edit_message_text(
                help_message,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        
        elif call.data == "menu_account":
            user = call.from_user
            stats = get_user_stats(user.id)
            
            account_message = f"""
👤 **حساب کاربری**

🆔 **شناسه کاربری:** `{user.id}`
👤 **نام:** {user.first_name or 'نامشخص'}
📊 **تعداد ویدیوهای دانلود شده:** {stats['success']}
📈 **تعداد کل درخواست‌ها:** {stats['total']}
            """
            markup = create_main_menu()
            bot.edit_message_text(
                account_message,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        
        elif call.data == "menu_stats":
            user_id = call.from_user.id
            stats = get_user_stats(user_id)
            videos = get_user_videos(user_id, limit=None)
            
            stats_message = f"""
📊 **آمار شما**

✅ **ویدیوهای موفق:** {stats['success']}
📈 **کل درخواست‌ها:** {stats['total']}
📹 **تعداد ویدیوهای دانلود شده:** {len(videos)}

🎯 **نرخ موفقیت:** {(stats['success'] / stats['total'] * 100) if stats['total'] > 0 else 0:.1f}%
            """
            markup = create_main_menu()
            bot.edit_message_text(
                stats_message,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        
        elif call.data == "menu_about":
            about_message = """
ℹ️ **درباره ربات**

🤖 **دانلودر اختصاصی اینستاگرام**
این ربات با معماری چندمسیره، لینک‌های عمومی اینستاگرام را تحلیل و ویدیو را با بهترین کیفیت ممکن در تلگرام تحویل می‌دهد.

✨ **مزایا:**
• ⚡ انتخاب خودکار سریع‌ترین روش دانلود
• 🧠 بازیابی و پاکسازی کپشن اصلی محتوا
• 🔐 عدم ذخیره‌سازی دائمی؛ فقط تاریخچه حداقلی برای تجربه بهتر
• 🛰️ تلاش مجدد هوشمند هنگام بروز خطا

🛠️ **فناوری‌ها:**
• Python + TeleBot
• yt-dlp، requests، BeautifulSoup

💡 کافیست لینک را ارسال کنید؛ بقیه مراحل خودکار انجام می‌شود.
            """
            markup = create_main_menu()
            bot.edit_message_text(
                about_message,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        
        # پاسخ به callback
        bot.answer_callback_query(call.id)
    
    except Exception as e:
        logger.error(f"خطا در callback: {e}")
        bot.answer_callback_query(call.id, "❌ خطایی رخ داد!")


@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_message(message):
    """پردازش پیام‌های کاربر (فقط پیام‌های غیر command)"""
    if not guard_user_access(message):
        return
    text = message.text
    
    # پردازش دکمه‌های کیبورد
    if text == "👤 حساب کاربری":
        try:
            user = message.from_user
            stats = get_user_stats(user.id)
            account_message = f"""
👤 **حساب کاربری**

🆔 **شناسه کاربری:** `{user.id}`
👤 **نام:** {user.first_name or 'نامشخص'}
📊 **تعداد ویدیوهای دانلود شده:** {stats['success']}
📈 **تعداد کل درخواست‌ها:** {stats['total']}
            """
            markup = create_reply_keyboard()
            bot.reply_to(message, account_message, parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            logger.error(f"خطا در نمایش حساب کاربری: {e}")
            markup = create_reply_keyboard()
            bot.reply_to(message, "❌ خطا در نمایش اطلاعات حساب کاربری. لطفاً دوباره تلاش کنید.", reply_markup=markup)
        return
    
    elif text == "📊 آمار":
        user_id = message.from_user.id
        stats = get_user_stats(user_id)
        videos = get_user_videos(user_id, limit=None)  # تمام ویدیوها
        
        stats_message = f"""
📊 **آمار شما**

✅ **ویدیوهای موفق:** {stats['success']}
📈 **کل درخواست‌ها:** {stats['total']}
📹 **تعداد کل ویدیوهای دانلود شده:** {len(videos)}

🎯 **نرخ موفقیت:** {(stats['success'] / stats['total'] * 100) if stats['total'] > 0 else 0:.1f}%

💡 این آمار از ابتدای استفاده شما از ربات محاسبه شده است.
        """
        markup = create_reply_keyboard()
        bot.reply_to(message, stats_message, parse_mode='Markdown', reply_markup=markup)
        return
    
    elif text == "ℹ️ درباره ربات":
        about_message = """
ℹ️ **درباره ربات**

🤖 **دانلودر اختصاصی اینستاگرام**
این ربات با معماری چندمسیره، لینک‌های عمومی اینستاگرام را تحلیل و ویدیو را با بهترین کیفیت ممکن در تلگرام تحویل می‌دهد.

✨ **مزایا:**
• ⚡ انتخاب خودکار سریع‌ترین روش دانلود
• 🧠 بازیابی و پاکسازی کپشن اصلی محتوا
• 🔐 عدم ذخیره‌سازی دائمی؛ فقط تاریخچه حداقلی برای تجربه بهتر
• 🛰️ تلاش مجدد هوشمند هنگام بروز خطا

🛠️ **فناوری‌ها:**
• Python + TeleBot
• yt-dlp، requests، BeautifulSoup

💡 کافیست لینک را ارسال کنید؛ بقیه مراحل خودکار انجام می‌شود.
        """
        markup = create_reply_keyboard()
        bot.reply_to(message, about_message, parse_mode='Markdown', reply_markup=markup)
        return
    
    
    if not text:
        # اگر پیام بدون متن است، منو را نمایش بده
        menu_message = "📱 **منوی ربات**\n\nگزینه مورد نظر را انتخاب کنید:"
        markup = create_reply_keyboard()
        bot.reply_to(message, menu_message, parse_mode='Markdown', reply_markup=markup)
        return
    
    # بررسی اینکه آیا کاربر شماره ردیف ارسال کرده (برای دانلود مجدد)
    if text.strip().isdigit():
        user_id = message.from_user.id
        index = int(text.strip())
        logger.info(f"🔍 درخواست دانلود مجدد ویدیو شماره {index} از کاربر {user_id}")
        
        # دریافت اطلاعات ویدیو
        video_info = get_user_video_by_index(user_id, index)
        
        if not video_info or not video_info.get('url'):
            markup = create_reply_keyboard()
            videos_count = len(get_user_videos(user_id, limit=None))
            if videos_count == 0:
                bot.reply_to(message, "❌ شما هنوز هیچ ویدیویی دانلود نکرده‌اید.\n\n💡 ابتدا یک ویدیو دانلود کنید.", reply_markup=markup)
            else:
                bot.reply_to(message, f"❌ ویدیو شماره {index} پیدا نشد.\n\n💡 لطفاً شماره معتبری از 1 تا {videos_count} ارسال کنید.", reply_markup=markup)
            return
        
        video_url = video_info['url']
        logger.info(f"✅ URL پیدا شد: {video_url}")
        
        # استفاده از همان منطق دانلود عادی
        status_message = bot.reply_to(message, f"⏳ در حال دانلود مجدد ویدیو شماره {index}...")
        
        try:
            # دانلود جدید (بدون بررسی کش)
            if is_instagram_url(video_url):
                bot.edit_message_text("⏳ در حال پردازش...",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id)
                logger.info(f"🚀 شروع دانلود اینستاگرام: {video_url}")
                result = download_instagram_video_advanced(video_url)
            else:
                bot.edit_message_text("❌ لینک ویدیو معتبر نیست.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id)
                return
            
            if not result:
                logger.error(f"❌ دانلود ناموفق برای URL: {video_url}")
                bot.edit_message_text("❌ خطا در دانلود ویدیو. لطفاً دوباره تلاش کنید.\n\n💡 ممکن است ویدیو حذف شده یا خصوصی باشد.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id)
                log_request(user_id, video_url, 'error', None, 'Download failed')
                return
            
            video_data, metadata = result
            method_used = metadata.get('method', 'instagram')
            logger.info(f"✅ دانلود موفق با روش: {method_used}")
            
            # بررسی حجم
            video_data.seek(0, 2)
            file_size = video_data.tell()
            video_data.seek(0)
            logger.info(f"📦 حجم فایل: {file_size / (1024*1024):.2f} MB")
            
            max_size = 50 * 1024 * 1024  # 50MB
            if file_size > max_size:
                bot.edit_message_text("❌ حجم ویدیو بیش از 50 مگابایت است و نمی‌تواند ارسال شود.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id)
                log_request(user_id, video_url, 'error', method_used, 'File too large')
                return
            
            bot.edit_message_text("📤 در حال ارسال ویدیو...",
                                chat_id=status_message.chat.id,
                                message_id=status_message.message_id)
            
            # ذخیره موقت - برای ویدیوهای بزرگتر از 10MB حتماً در فایل ذخیره کن
            temp_file = None
            if file_size > 10 * 1024 * 1024:  # اگر بیشتر از 10MB است، در فایل ذخیره کن
                temp_file = TEMP_DIR / f"video_{int(time.time())}.mp4"
                write_temp_video(video_data, temp_file)
                logger.info(f"💾 فایل موقت ذخیره شد: {temp_file}")
            
            # ساخت caption نهایی
            final_caption = resolve_caption(metadata, video_url)
            
            # ارسال ویدیو با retry
            try:
                # برای ویدیوهای بزرگ، حتماً در فایل ذخیره کن
                if file_size > 10 * 1024 * 1024:  # بیشتر از 10MB
                    if not temp_file:
                        temp_file = TEMP_DIR / f"video_{int(time.time())}.mp4"
                        write_temp_video(video_data, temp_file)
                        logger.info(f"ویدیو بزرگ در فایل ذخیره شد: {temp_file}")
                    elif not temp_file.exists():
                        write_temp_video(video_data, temp_file)
                    
                    # ارسال از فایل (بدون markup اولیه)
                    with open(temp_file, 'rb') as f:
                        result_msg = send_video_with_retry(
                            chat_id=message.chat.id,
                            video_source=f,
                            caption=final_caption,
                            reply_to_message_id=message.message_id,
                            reply_markup=None
                        )
                else:
                    # ارسال از BytesIO (بدون markup اولیه)
                    result_msg = send_video_with_retry(
                        chat_id=message.chat.id,
                        video_source=video_data,
                        caption=final_caption,
                        reply_to_message_id=message.message_id,
                        reply_markup=None
                    )
                
                # بعد از ارسال موفق، دکمه استخراج آهنگ را اضافه می‌کنیم
                if result_msg:
                    audio_button = create_audio_extract_button(video_url, result_msg.message_id)
                    try:
                        bot.edit_message_reply_markup(
                            chat_id=message.chat.id,
                            message_id=result_msg.message_id,
                            reply_markup=audio_button
                        )
                    except Exception as e:
                        logger.warning(f"خطا در اضافه کردن دکمه استخراج آهنگ: {e}")
                        # اگر خطا داد، یک پیام جداگانه با دکمه می‌فرستیم
                        try:
                            bot.send_message(
                                message.chat.id,
                                "🎵 برای استخراج آهنگ از ویدیو، روی دکمه زیر کلیک کنید:",
                                reply_to_message_id=result_msg.message_id,
                                reply_markup=audio_button
                            )
                        except:
                            pass
                
                logger.info("✅ ویدیو با موفقیت ارسال شد")
            except Exception as send_error:
                logger.error(f"❌ خطا در ارسال ویدیو بعد از {3} تلاش: {send_error}", exc_info=True)
                bot.edit_message_text("❌ خطا در ارسال ویدیو. لطفاً دوباره تلاش کنید.\n\n💡 ممکن است ویدیو خیلی بزرگ باشد یا اتصال اینترنت مشکل داشته باشد.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id)
                return
            
            # حذف پیام وضعیت
            try:
                bot.delete_message(chat_id=status_message.chat.id, message_id=status_message.message_id)
            except:
                pass
            
            # ثبت لاگ درخواست موفق
            log_request(user_id, video_url, 'success', method_used)
            return
            
        except Exception as e:
            logger.error(f"❌ خطا در دانلود مجدد: {e}", exc_info=True)
            try:
                if status_message:
                    error_msg = str(e)[:150] if len(str(e)) > 150 else str(e)
                    bot.edit_message_text(f"❌ خطایی رخ داد:\n{error_msg}\n\nلطفاً دوباره تلاش کنید.",
                                        chat_id=status_message.chat.id,
                                        message_id=status_message.message_id)
                else:
                    markup = create_reply_keyboard()
                    bot.reply_to(message, "❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.", reply_markup=markup)
            except Exception as e2:
                logger.error(f"❌ خطا در ارسال پیام خطا: {e2}")
            return
    
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        markup = create_reply_keyboard()
        bot.reply_to(message, "❌ لطفاً یک لینک معتبر از اینستاگرام ارسال کنید.", reply_markup=markup)
        return
    
    url = urls[0]
    status_message = bot.reply_to(message, "⏳ در حال پردازش لینک...")
    
    try:
        # دانلود جدید (بدون بررسی کش)
        video_data = None
        metadata = {}
        method_used = None
        
        if is_instagram_url(url):
            bot.edit_message_text("⏳ در حال پردازش...",
                                chat_id=status_message.chat.id,
                                message_id=status_message.message_id)
            result = download_instagram_video_advanced(url)
            if result:
                video_data, metadata = result
                method_used = metadata.get('method', 'instagram')
        else:
            markup = create_reply_keyboard()
            try:
                bot.edit_message_text("❌ لینک ارسالی معتبر نیست. لطفاً لینک اینستاگرام ارسال کنید.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id,
                                    reply_markup=markup)
            except:
                bot.send_message(message.chat.id, "❌ لینک ارسالی معتبر نیست. لطفاً لینک اینستاگرام ارسال کنید.", reply_markup=markup)
            log_request(message.from_user.id, url, 'error', None, 'Invalid URL')
            return
        
        if not video_data:
            markup = create_reply_keyboard()
            try:
                bot.edit_message_text("❌ خطا در دانلود ویدیو. لطفاً دوباره تلاش کنید.\n\n💡 ممکن است ویدیو خصوصی باشد یا لینک معتبر نباشد.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id,
                                    reply_markup=markup)
            except:
                bot.send_message(message.chat.id, "❌ خطا در دانلود ویدیو. لطفاً دوباره تلاش کنید.", reply_markup=markup)
            log_request(message.from_user.id, url, 'error', method_used, 'Download failed')
            return
        
        # بررسی حجم
        video_data.seek(0, 2)  # رفتن به انتهای فایل
        file_size = video_data.tell()
        video_data.seek(0)  # بازگشت به ابتدا
        
        max_size = 50 * 1024 * 1024  # 50MB
        
        if file_size > max_size:
            markup = create_reply_keyboard()
            try:
                bot.edit_message_text(
                    f"❌ حجم ویدیو ({file_size / (1024*1024):.2f} MB) بیش از حد مجاز تلگرام (50 MB) است.",
                    chat_id=status_message.chat.id,
                    message_id=status_message.message_id,
                    reply_markup=markup
                )
            except:
                bot.send_message(message.chat.id, f"❌ حجم ویدیو ({file_size / (1024*1024):.2f} MB) بیش از حد مجاز تلگرام (50 MB) است.", reply_markup=markup)
            log_request(message.from_user.id, url, 'error', method_used, 'File too large')
            return
        
        # ارسال ویدیو
        bot.edit_message_text("📤 در حال ارسال ویدیو...",
                            chat_id=status_message.chat.id,
                            message_id=status_message.message_id)
        
        # ذخیره موقت برای کش
        temp_file = None
        if is_instagram_url(url):
            shortcode, content_type = extract_instagram_shortcode(url)
            if shortcode:
                temp_file = TEMP_DIR / f"instagram_{shortcode}.mp4"
                write_temp_video(video_data, temp_file)
        
        # ساخت caption نهایی
        final_caption = resolve_caption(metadata, url)
        
        # ارسال ویدیو با retry
        reply_keyboard = create_reply_keyboard()
        try:
            # برای ویدیوهای بزرگ، حتماً در فایل ذخیره کن
            if file_size > 10 * 1024 * 1024:  # بیشتر از 10MB
                if not temp_file:
                    temp_file = TEMP_DIR / f"video_{int(time.time())}.mp4"
                    write_temp_video(video_data, temp_file)
                    logger.info(f"ویدیو بزرگ در فایل ذخیره شد: {temp_file}")
                elif not temp_file.exists():
                    write_temp_video(video_data, temp_file)
                
                # ارسال از فایل
                with open(temp_file, 'rb') as f:
                    result_msg = send_video_with_retry(
                        chat_id=message.chat.id,
                        video_source=f,
                        caption=final_caption,
                        reply_to_message_id=message.message_id,
                        reply_markup=None  # ابتدا بدون markup ارسال می‌کنیم
                    )
            else:
                # ارسال از BytesIO
                result_msg = send_video_with_retry(
                    chat_id=message.chat.id,
                    video_source=video_data,
                    caption=final_caption,
                    reply_to_message_id=message.message_id,
                    reply_markup=None  # ابتدا بدون markup ارسال می‌کنیم
                )
            
            # بعد از ارسال موفق، دکمه استخراج آهنگ را اضافه می‌کنیم
            if result_msg:
                audio_button = create_audio_extract_button(url, result_msg.message_id)
                try:
                    bot.edit_message_reply_markup(
                        chat_id=message.chat.id,
                        message_id=result_msg.message_id,
                        reply_markup=audio_button
                    )
                except Exception as e:
                    logger.warning(f"خطا در اضافه کردن دکمه استخراج آهنگ: {e}")
                    # اگر خطا داد، یک پیام جداگانه با دکمه می‌فرستیم
                    try:
                        bot.send_message(
                            message.chat.id,
                            "🎵 برای استخراج آهنگ از ویدیو، روی دکمه زیر کلیک کنید:",
                            reply_to_message_id=result_msg.message_id,
                            reply_markup=audio_button
                        )
                    except:
                        pass
                if temp_file and temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception as cleanup_error:
                        logger.warning(f"⚠️ خطا در حذف فایل موقت {temp_file}: {cleanup_error}")
            logger.info("✅ ویدیو با موفقیت ارسال شد")
        except Exception as send_error:
            logger.error(f"❌ خطا در ارسال ویدیو بعد از {3} تلاش: {send_error}", exc_info=True)
            try:
                bot.edit_message_text("❌ خطا در ارسال ویدیو. لطفاً دوباره تلاش کنید.\n\n💡 ممکن است ویدیو خیلی بزرگ باشد یا اتصال اینترنت مشکل داشته باشد.",
                                    chat_id=status_message.chat.id,
                                    message_id=status_message.message_id)
            except:
                pass
            return
        
        bot.delete_message(chat_id=status_message.chat.id, message_id=status_message.message_id)
        
        # حذف فایل موقت در صورت وجود
        if temp_file and temp_file.exists():
            try:
                temp_file.unlink()
            except Exception as cleanup_error:
                logger.warning(f"⚠️ خطا در حذف فایل موقت {temp_file}: {cleanup_error}")
        
        # ثبت لاگ درخواست موفق
        log_request(message.from_user.id, url, 'success', method_used)
            
    except Exception as e:
        logger.error(f"خطا در پردازش: {e}")
        try:
            bot.edit_message_text("❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.",
                                chat_id=status_message.chat.id,
                                message_id=status_message.message_id)
            log_request(message.from_user.id, url, 'error', None, str(e))
        except:
            pass


def main():
    """تابع اصلی"""
    logger.info("🚀 در حال راه‌اندازی ربات...")
    init_storage()
    
    # تنظیم منوی همبرگری
    logger.info("📱 در حال تنظیم منوی همبرگری...")
    setup_bot_commands()
    
    # بررسی اینکه آیا منو تنظیم شد
    try:
        # بررسی با استفاده از API مستقیم
        import requests
        url = f"https://api.telegram.org/bot{bot.token}/getMyCommands"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok') and data.get('result'):
                commands = data['result']
                logger.info(f"✅ منوی همبرگری با {len(commands)} دستور تنظیم شد")
                for cmd in commands:
                    logger.info(f"   - /{cmd.get('command')}: {cmd.get('description')}")
            else:
                logger.warning("⚠️ منوی همبرگری تنظیم نشد، تلاش مجدد...")
                setup_bot_commands()
        else:
            logger.warning(f"⚠️ نتوانست منوی همبرگری را بررسی کند: {response.status_code}")
    except Exception as e:
        logger.warning(f"⚠️ نتوانست منوی همبرگری را بررسی کند: {e}")
    
    logger.info("✅ ربات آماده است!")
    
    # اجرای ربات با error handling بهتر
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, none_stop=True, skip_pending=True)
        except Exception as e:
            logger.error(f"خطا در polling: {e}")
            logger.info("تلاش مجدد در 5 ثانیه...")
            time.sleep(5)


if __name__ == '__main__':
    main()

