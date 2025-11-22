#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات مدیریت برای کنترل ربات دانلودر اینستاگرام
"""

import json
import logging
import os
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import telebot
from telebot import types
import jdatetime

# ===== تنظیمات پایه =====
MANAGER_BOT_TOKEN = "7786839303:AAGTnAnGe4k47TZeNcTZ6M1Sy63t6tt6bfs"
DOWNLOADER_BOT_TOKEN = "8414466743:AAFeOrB3ElfZKrashXksHjGaqllHdpGUn3U"
API_ID = 29677125
API_HASH = "93a6686433f91faa648541b3e57b2883"

ADMIN_PASSWORD = os.environ.get("MANAGER_ADMIN_PASSWORD", "FARA2026")
SESSION_TIMEOUT_SECONDS = 3600  # یک ساعت

REQUEST_HISTORY_PATH = Path("request_history.json")
MANAGER_STATE_PATH = Path("manager_state.json")
USERS_DATA_PATH = Path("data/users.json")

# ===== لاگ =====
logging.basicConfig(
    format="%(asctime)s - manager_bot - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("manager_bot")

# ===== وضعیت‌های در حافظه =====
bot = telebot.TeleBot(MANAGER_BOT_TOKEN)
AUTH_SESSIONS: Dict[int, float] = {}
USER_STATES: Dict[int, str] = {}  # برای ذخیره وضعیت کاربران (مثلاً "adding_channel", "removing_channel")

# ===== توابع کمکی وضعیت =====
def load_manager_state() -> Dict:
    """بارگذاری تنظیمات مدیریتی از فایل JSON"""
    default_state = {
        "blocked_users": [],
        "maintenance_mode": False,
        "notes": "",
        "required_channels": [],  # لیست کانال‌های اجباری [{"username": "@channel", "title": "عنوان"}]
        "last_update": datetime.now(timezone.utc).isoformat(),
    }
    if not MANAGER_STATE_PATH.exists():
        save_manager_state(default_state)
        return default_state

    try:
        with open(MANAGER_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "blocked_users" not in data:
            data["blocked_users"] = []
        if "maintenance_mode" not in data:
            data["maintenance_mode"] = False
        if "required_channels" not in data:
            # تبدیل required_channel قدیمی به required_channels جدید
            if "required_channel" in data and data["required_channel"]:
                data["required_channels"] = [{"username": data["required_channel"], "title": "کانال اجباری"}]
                del data["required_channel"]
            else:
                data["required_channels"] = []
        return data
    except Exception as exc:
        logger.error("خطا در خواندن manager_state.json: %s", exc)
        return default_state


def save_manager_state(state: Dict) -> None:
    """ذخیره وضعیت مدیریتی"""
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    MANAGER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANAGER_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_password(input_password: str) -> bool:
    """بررسی رمز ورود با لاگ‌های دقیق"""
    input_clean = input_password.strip()
    correct_password = ADMIN_PASSWORD.strip()
    
    # لاگ برای دیباگ
    logger.info(f"🔐 بررسی رمز - ورودی: '{input_clean}' (طول: {len(input_clean)}, bytes: {input_clean.encode('utf-8')})")
    logger.info(f"🔐 بررسی رمز - صحیح: '{correct_password}' (طول: {len(correct_password)}, bytes: {correct_password.encode('utf-8')})")
    
    match = input_clean == correct_password
    logger.info(f"🔐 نتیجه بررسی: {match}")
    
    return match


def format_jalali_date(dt: datetime) -> str:
    """تبدیل تاریخ میلادی به شمسی"""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y/%m/%d %H:%M")
    except:
        return str(dt)


def check_downloader_bot_status() -> Dict[str, any]:
    """بررسی دقیق وضعیت ربات دانلودر - فقط فعال یا غیرفعال"""
    try:
        # مرحله 1: بررسی توکن و اطلاعات ربات با getMe
        url_getme = f"https://api.telegram.org/bot{DOWNLOADER_BOT_TOKEN}/getMe"
        try:
            response_getme = requests.get(url_getme, timeout=5)
        except requests.exceptions.ConnectionError:
            return {"active": False, "error": "خطا در اتصال به اینترنت"}
        except requests.exceptions.Timeout:
            return {"active": False, "error": "Timeout - سرور پاسخ نمی‌دهد"}
        
        if response_getme.status_code != 200:
            return {"active": False, "error": "خطا در اتصال به API"}
        
        data_getme = response_getme.json()
        if not data_getme.get('ok'):
            error_code = data_getme.get('error_code', '')
            if error_code == 401:
                return {"active": False, "error": "توکن نامعتبر"}
            return {"active": False, "error": "توکن معتبر نیست"}
        
        bot_info = data_getme['result']
        username = bot_info.get('username', 'N/A')
        first_name = bot_info.get('first_name', 'N/A')
        
        # مرحله 2: بررسی اینکه آیا ربات واقعاً در حال اجرا است
        # استفاده از getUpdates برای بررسی اینکه آیا ربات polling می‌کند
        url_updates = f"https://api.telegram.org/bot{DOWNLOADER_BOT_TOKEN}/getUpdates"
        try:
            # استفاده از offset منفی و timeout کوتاه برای بررسی سریع
            response_updates = requests.get(url_updates, params={"offset": -1, "limit": 1, "timeout": 1}, timeout=3)
            
            if response_updates.status_code == 200:
                data_updates = response_updates.json()
                if data_updates.get('ok'):
                    # اگر getUpdates موفق بود، ربات فعال است
                    return {"active": True, "username": username, "first_name": first_name}
                else:
                    error_code = data_updates.get('error_code')
                    if error_code == 409:
                        # خطای 409 یعنی ربات در حال polling است - این یعنی ربات فعال است!
                        return {"active": True, "username": username, "first_name": first_name}
                    else:
                        # خطای دیگر - ربات غیرفعال است
                        return {"active": False, "error": "ربات در حال اجرا نیست", "username": username, "first_name": first_name}
            else:
                # اگر status code 200 نبود، ربات ممکن است غیرفعال باشد
                return {"active": False, "error": "ربات در حال اجرا نیست", "username": username, "first_name": first_name}
                
        except requests.exceptions.Timeout:
            # Timeout در getUpdates - این ممکن است طبیعی باشد اگر ربات در حال polling باشد
            # اما برای اطمینان بیشتر، بررسی می‌کنیم که آیا می‌توانیم webhook را چک کنیم
            try:
                url_webhook = f"https://api.telegram.org/bot{DOWNLOADER_BOT_TOKEN}/getWebhookInfo"
                response_webhook = requests.get(url_webhook, timeout=3)
                if response_webhook.status_code == 200:
                    data_webhook = response_webhook.json()
                    if data_webhook.get('ok'):
                        webhook_info = data_webhook.get('result', {})
                        if webhook_info.get('url'):
                            # اگر webhook تنظیم شده باشد، ربات فعال است
                            return {"active": True, "username": username, "first_name": first_name}
                # اگر webhook هم تنظیم نشده باشد، ربات غیرفعال است
                return {"active": False, "error": "ربات در حال اجرا نیست", "username": username, "first_name": first_name}
            except:
                # اگر webhook هم کار نکرد، ربات غیرفعال است
                return {"active": False, "error": "ربات در حال اجرا نیست", "username": username, "first_name": first_name}
        except Exception as e:
            logger.debug(f"خطا در getUpdates: {e}")
            # اگر خطای دیگری رخ داد، ربات غیرفعال است
            return {"active": False, "error": "ربات در حال اجرا نیست", "username": username, "first_name": first_name}
        
    except Exception as e:
        logger.error(f"خطا در بررسی وضعیت ربات: {e}", exc_info=True)
        return {"active": False, "error": "خطا در بررسی وضعیت"}


def get_user_username(user_id: int) -> str:
    """دریافت @username کاربر از طریق API ربات دانلودر"""
    try:
        # استفاده از توکن ربات دانلودر برای گرفتن اطلاعات کاربر
        url = f"https://api.telegram.org/bot{DOWNLOADER_BOT_TOKEN}/getChat"
        response = requests.post(url, json={"chat_id": user_id}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                result = data['result']
                username = result.get('username')
                if username:
                    return f"@{username}"
                first_name = result.get('first_name', '')
                last_name = result.get('last_name', '')
                name = f"{first_name} {last_name}".strip()
                if name:
                    return name
        # اگر نتوانست از ربات دانلودر بگیرد، از ربات مدیریت امتحان کن
        url = f"https://api.telegram.org/bot{MANAGER_BOT_TOKEN}/getChat"
        response = requests.post(url, json={"chat_id": user_id}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                result = data['result']
                username = result.get('username')
                if username:
                    return f"@{username}"
                first_name = result.get('first_name', '')
                last_name = result.get('last_name', '')
                name = f"{first_name} {last_name}".strip()
                if name:
                    return name
        return f"ID: {user_id}"
    except Exception as e:
        logger.debug(f"خطا در دریافت اطلاعات کاربر {user_id}: {e}")
        return f"ID: {user_id}"


# ===== توابع کمکی تاریخچه =====
def load_request_history() -> Dict[str, List[Dict]]:
    if not REQUEST_HISTORY_PATH.exists():
        return {}
    try:
        with open(REQUEST_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.error("خطا در خواندن request_history.json: %s", exc)
        return {}


def calculate_stats() -> Dict[str, int]:
    """محاسبه آمار کلی از request_history"""
    history = load_request_history()
    total_requests = 0
    success_requests = 0
    error_requests = 0
    unique_users = set()

    for user_id_str, entries in history.items():
        if not isinstance(entries, list) or not entries:
            continue
        
        try:
            user_id = int(user_id_str)
            unique_users.add(user_id)
        except (ValueError, TypeError):
            continue
        
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            total_requests += 1
            status = entry.get("status", "").lower()
            if status == "success":
                success_requests += 1
            else:
                error_requests += 1

    return {
        "users": len(unique_users),
        "total": total_requests,
        "success": success_requests,
        "errors": error_requests,
    }


def get_recent_users(limit: int = 10) -> List[Dict]:
    """دریافت آخرین کاربران بر اساس زمان آخرین درخواست"""
    history = load_request_history()
    recent_list = []
    
    for user_id_str, entries in history.items():
        if not isinstance(entries, list) or not entries:
            continue
        
        try:
            user_id = int(user_id_str)
        except (ValueError, TypeError):
            continue
        
        # پیدا کردن جدیدترین entry (اولین entry در لیست یا جدیدترین بر اساس created_at)
        latest_entry = None
        latest_time = None
        
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            
            created_at_str = entry.get("created_at")
            if not created_at_str:
                continue
            
            try:
                # تبدیل string به datetime
                if isinstance(created_at_str, str):
                    # پشتیبانی از فرمت‌های مختلف
                    if 'T' in created_at_str or ' ' in created_at_str:
                        created_at_str = created_at_str.replace('Z', '+00:00')
                        try:
                            entry_time = datetime.fromisoformat(created_at_str)
                        except:
                            # اگر فرمت ISO نبود، سعی کن parse کن
                            try:
                                entry_time = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
                            except:
                                continue
                    else:
                        continue
                else:
                    continue
                
                if latest_time is None or entry_time > latest_time:
                    latest_time = entry_time
                    latest_entry = entry
            except Exception as e:
                logger.debug(f"خطا در parse کردن تاریخ: {e}")
                continue
        
        if latest_entry and latest_time:
            recent_list.append(
                {
                    "user_id": user_id,
                    "last_url": latest_entry.get("url", "N/A"),
                    "status": latest_entry.get("status", "unknown"),
                    "created_at": latest_time,
                }
            )

    # مرتب‌سازی بر اساس زمان (جدیدترین اول)
    recent_list.sort(key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return recent_list[:limit]


def get_user_details(target_user_id: str) -> Optional[Dict]:
    history = load_request_history()
    entries = history.get(str(target_user_id))
    if not entries:
        return None

    total = len(entries)
    successes = sum(1 for entry in entries if entry.get("status") == "success")
    last_entry = entries[0]
    return {
        "total": total,
        "success": successes,
        "last_url": last_entry.get("url"),
        "last_status": last_entry.get("status"),
        "created_at": last_entry.get("created_at"),
    }


def load_users() -> dict:
    """لود لیست کاربران از فایل JSON مشترک"""
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


def get_all_users() -> List[Dict]:
    """دریافت لیست تمام کاربران از users.json"""
    users_data = load_users()
    users_list = []
    
    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)
            users_list.append({
                'user_id': user_id,
                'username': user_info.get('username'),
                'first_name': user_info.get('first_name'),
                'last_name': user_info.get('last_name'),
                'registered_at': user_info.get('registered_at'),
                'last_seen': user_info.get('last_seen')
            })
        except (ValueError, TypeError) as e:
            logger.debug(f"خطا در پردازش کاربر {user_id_str}: {e}")
            continue
    
    # مرتب‌سازی بر اساس زمان ثبت (جدیدترین اول)
    users_list.sort(
        key=lambda u: u.get('registered_at') or '',
        reverse=True
    )
    return users_list


# ===== احراز هویت =====
def is_authenticated(user_id: int) -> bool:
    expiry = AUTH_SESSIONS.get(user_id)
    if not expiry:
        return False
    if time.time() > expiry:
        AUTH_SESSIONS.pop(user_id, None)
        return False
    return True


def require_auth(func):
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        if not is_authenticated(user_id):
            bot.reply_to(
                message,
                "🚫 ابتدا با دستور `/login <password>` وارد شوید.",
                parse_mode="Markdown",
            )
            return
        return func(message, *args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


def refresh_session(user_id: int):
    AUTH_SESSIONS[user_id] = time.time() + SESSION_TIMEOUT_SECONDS


# ===== رابط کاربری =====
def build_main_menu() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 آمار کلی")
    markup.add("👤 لیست تمام کاربران", "🔗 مدیریت کانال‌ها")
    markup.add("⚙️ وضعیت ربات دانلودر")
    markup.add("🚫 مسدود کردن", "✅ رفع انسداد")
    markup.add("🔐 ورود مجدد")
    return markup


def format_datetime(value: Optional[str]) -> str:
    """فرمت کردن تاریخ به شمسی"""
    if not value:
        return "نامشخص"
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            dt = value
        return format_jalali_date(dt)
    except:
        return str(value)
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


# ===== هندلرها =====
@bot.message_handler(commands=["start"])
def start_cmd(message):
    # ثبت کاربر در فایل users.json
    user = message.from_user
    register_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    bot.reply_to(
        message,
        (
            "🤖 به کنسول مدیریت ربات دانلودر خوش آمدید.\n"
            "برای شروع، دستور `/login <password>` را ارسال کنید.\n"
            "پس از ورود، می‌توانید از منو یا دستورات `/help` استفاده کنید."
        ),
        parse_mode="Markdown",
        reply_markup=build_main_menu(),
    )


@bot.message_handler(commands=["login"])
def login_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        bot.reply_to(message, "🔐 لطفاً پسورد را این‌گونه وارد کنید:\n`/login your_password`", parse_mode="Markdown")
        return
    password = parts[1]
    if not check_password(password):
        logger.warning("تلاش ناموفق برای ورود از کاربر %s", message.from_user.id)
        bot.reply_to(message, "❌ پسورد اشتباه است.")
        return

    refresh_session(message.from_user.id)
    bot.reply_to(
        message,
        "✅ **خوش آمدید!**\n\n🔓 دسترسی مدیریتی برای شما باز شد.\n\n⏰ جلسۀ مدیریتی تا ۶۰ دقیقه فعال است.\n\n💡 از منوی زیر یا دستورات برای مدیریت ربات استفاده کنید.",
        parse_mode="Markdown",
        reply_markup=build_main_menu(),
    )


@bot.message_handler(commands=["logout"])
@require_auth
def logout_cmd(message):
    AUTH_SESSIONS.pop(message.from_user.id, None)
    bot.reply_to(message, "✅ از حساب مدیریتی خارج شدید.")


@bot.message_handler(commands=["start"])
@require_auth
def start_cmd_authenticated(message):
    # ثبت کاربر در فایل users.json (به‌روزرسانی last_seen)
    user = message.from_user
    register_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    bot.reply_to(
        message,
        "✅ **خوش آمدید به پنل مدیریت!**\n\n💡 از منوی زیر یا دستورات برای مدیریت ربات استفاده کنید.",
        parse_mode="Markdown",
        reply_markup=build_main_menu(),
    )


@bot.message_handler(commands=["help"])
@require_auth
def help_cmd(message):
    bot.reply_to(
        message,
        (
            "📖 **دستورات مدیریتی:**\n"
            "• `/stats` دریافت آمار کلی\n"
            "• `/users` نمایش لیست تمام کاربران ثبت شده\n"
            "• `/user <id>` جزئیات کاربر\n"
            "• `/block <id>` مسدود کردن کاربر\n"
            "• `/unblock <id>` رفع انسداد کاربر\n"
            "• `/blocked` فهرست کاربران مسدود\n"
            "• `/maintenance on|off` فعال/غیرفعال کردن حالت نگهداری\n"
            "• `/channel <@username>` تنظیم کانال اجباری (جوین اجباری)\n"
            "• `/channel off` غیرفعال کردن جوین اجباری\n"
            "• `/status` نمایش وضعیت فعلی ربات دانلودر"
        ),
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["stats"])
@require_auth
def stats_cmd(message):
    """نمایش آمار دقیق ربات دانلودر"""
    try:
        stats = calculate_stats()
        history = load_request_history()
        
        # شمارش دقیق کاربران از request_history
        unique_user_ids = set()
        for user_id_str in history.keys():
            try:
                user_id = int(user_id_str)
                entries = history.get(user_id_str, [])
                if isinstance(entries, list) and len(entries) > 0:
                    unique_user_ids.add(user_id)
            except (ValueError, TypeError):
                continue
        
        total_members_history = len(unique_user_ids)
        
        # شمارش کاربران از users.json
        all_users = get_all_users()
        total_members_registered = len(all_users)
        
        # محاسبه نرخ موفقیت
        success_rate = 0.0
        if stats['total'] > 0:
            success_rate = (stats['success'] / stats['total']) * 100
        
        # محاسبه آمار اضافی
        error_rate = 0.0
        if stats['total'] > 0:
            error_rate = (stats['errors'] / stats['total']) * 100
        
        # ساخت پیام با جزئیات بیشتر
        lines = ["📊 **آمار دقیق ربات دانلودر**\n"]
        
        lines.append("👥 **کاربران:**")
        lines.append(f"   • ثبت شده در سیستم: {total_members_registered}")
        lines.append(f"   • فعال در تاریخچه: {total_members_history}")
        
        lines.append("\n📨 **درخواست‌ها:**")
        lines.append(f"   • کل درخواست‌ها: {stats['total']}")
        lines.append(f"   • موفق: {stats['success']} ({success_rate:.1f}%)")
        lines.append(f"   • خطا: {stats['errors']} ({error_rate:.1f}%)")
        
        # اگر کاربران ثبت شده وجود دارند، نمایش آخرین کاربران
        if all_users:
            lines.append("\n🆕 **آخرین کاربران ثبت شده:**")
            for idx, user in enumerate(all_users[:5], 1):  # 5 کاربر آخر
                user_id = user.get('user_id', 'N/A')
                username = user.get('username')
                first_name = user.get('first_name', '')
                
                if username:
                    display_name = f"@{username}"
                elif first_name:
                    display_name = first_name
                else:
                    display_name = f"ID: {user_id}"
                
                # Escape کردن کاراکترهای خاص Markdown
                display_name_safe = display_name.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
                
                registered_at = user.get('registered_at')
                if registered_at:
                    reg_date = format_jalali_date(registered_at)
                else:
                    reg_date = "نامشخص"
                
                lines.append(f"   {idx}. {display_name_safe} - {reg_date}")
        
        response = "\n".join(lines)
        
        try:
            bot.reply_to(message, response, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"خطا در ارسال آمار با Markdown: {e}")
            # ارسال بدون Markdown در صورت خطا
            lines_plain = ["📊 آمار دقیق ربات دانلودر\n"]
            lines_plain.append(f"👥 کاربران:")
            lines_plain.append(f"   • ثبت شده در سیستم: {total_members_registered}")
            lines_plain.append(f"   • فعال در تاریخچه: {total_members_history}")
            lines_plain.append(f"\n📨 درخواست‌ها:")
            lines_plain.append(f"   • کل درخواست‌ها: {stats['total']}")
            lines_plain.append(f"   • موفق: {stats['success']} ({success_rate:.1f}%)")
            lines_plain.append(f"   • خطا: {stats['errors']} ({error_rate:.1f}%)")
            bot.reply_to(message, "\n".join(lines_plain))
    except Exception as e:
        logger.error(f"خطا در محاسبه آمار: {e}", exc_info=True)
        bot.reply_to(message, f"❌ خطا در دریافت آمار: {str(e)}")


@bot.message_handler(commands=["recent"])
@require_auth
def recent_cmd(message):
    recent_users = get_recent_users(limit=10)
    if not recent_users:
        bot.reply_to(message, "ℹ️ هیچ کاربر فعالی در تاریخچه یافت نشد.")
        return
    
    lines = ["👥 **۱۰ کاربر اخیر:**\n"]
    for idx, item in enumerate(recent_users, 1):
        try:
            user_id = item.get('user_id')
            if not user_id:
                continue
            
            username = get_user_username(user_id)
            created_at = item.get('created_at')
            
            if created_at:
                date_str = format_jalali_date(created_at)
            else:
                date_str = "نامشخص"
            
            status = item.get('status', 'unknown')
            status_emoji = "✅" if status.lower() == 'success' else "❌"
            
            # Escape کردن کاراکترهای خاص Markdown
            username_safe = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]').replace('(', '\\(').replace(')', '\\)')
            
            lines.append(
                f"{idx}. {username_safe}\n   📅 {date_str}\n   {status_emoji} {status}\n"
            )
        except Exception as e:
            logger.error(f"خطا در پردازش کاربر {idx}: {e}")
            continue
    
    if len(lines) == 1:  # فقط عنوان
        bot.reply_to(message, "ℹ️ هیچ کاربر فعالی در تاریخچه یافت نشد.")
        return
    
    try:
        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        # در صورت خطا، بدون Markdown ارسال کن
        logger.error(f"خطا در ارسال با Markdown: {e}")
        lines_plain = ["👥 ۱۰ کاربر اخیر:\n"]
        for idx, item in enumerate(recent_users, 1):
            try:
                user_id = item.get('user_id')
                username = get_user_username(user_id) if user_id else "نامشخص"
                created_at = item.get('created_at')
                date_str = format_jalali_date(created_at) if created_at else "نامشخص"
                status = item.get('status', 'unknown')
                status_emoji = "✅" if status.lower() == 'success' else "❌"
                lines_plain.append(f"{idx}. {username}\n   📅 {date_str}\n   {status_emoji} {status}\n")
            except:
                continue
        bot.reply_to(message, "\n".join(lines_plain))


@bot.message_handler(commands=["users"])
@require_auth
def users_cmd(message):
    """نمایش لیست تمام کاربران ثبت شده در users.json"""
    all_users = get_all_users()
    
    if not all_users:
        bot.reply_to(message, "ℹ️ هیچ کاربری در سیستم ثبت نشده است.")
        return
    
    # تقسیم لیست به صفحات (هر صفحه 20 کاربر)
    page_size = 20
    total_pages = (len(all_users) + page_size - 1) // page_size
    
    # اگر کاربر صفحه خاصی را درخواست کرده
    parts = message.text.split()
    page = 1
    if len(parts) > 1:
        try:
            page = int(parts[1])
            if page < 1:
                page = 1
            if page > total_pages:
                page = total_pages
        except ValueError:
            page = 1
    
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_users = all_users[start_idx:end_idx]
    
    lines = [f"👤 **لیست تمام کاربران** (صفحه {page}/{total_pages})\n"]
    lines.append(f"📊 **تعداد کل:** {len(all_users)} کاربر\n")
    
    for idx, user in enumerate(page_users, start=start_idx + 1):
        user_id = user.get('user_id', 'N/A')
        username = user.get('username')
        first_name = user.get('first_name', '')
        last_name = user.get('last_name', '')
        
        # ساخت نام نمایشی
        if username:
            display_name = f"@{username}"
        elif first_name or last_name:
            display_name = f"{first_name} {last_name}".strip()
        else:
            display_name = f"ID: {user_id}"
        
        # Escape کردن کاراکترهای خاص Markdown
        display_name_safe = display_name.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
        
        registered_at = user.get('registered_at')
        if registered_at:
            reg_date = format_jalali_date(registered_at)
        else:
            reg_date = "نامشخص"
        
        lines.append(f"{idx}. {display_name_safe}\n   🆔 `{user_id}`\n   📅 ثبت: {reg_date}")
    
    if total_pages > 1:
        lines.append(f"\n💡 برای مشاهده صفحه دیگر: `/users <شماره صفحه>`")
    
    try:
        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"خطا در ارسال لیست کاربران: {e}")
        # ارسال بدون Markdown در صورت خطا
        lines_plain = [f"👤 لیست تمام کاربران (صفحه {page}/{total_pages})\n"]
        lines_plain.append(f"📊 تعداد کل: {len(all_users)} کاربر\n")
        for idx, user in enumerate(page_users, start=start_idx + 1):
            user_id = user.get('user_id', 'N/A')
            username = user.get('username')
            first_name = user.get('first_name', '')
            last_name = user.get('last_name', '')
            display_name = f"@{username}" if username else f"{first_name} {last_name}".strip() or f"ID: {user_id}"
            registered_at = user.get('registered_at')
            reg_date = format_jalali_date(registered_at) if registered_at else "نامشخص"
            lines_plain.append(f"{idx}. {display_name} (ID: {user_id}) - ثبت: {reg_date}")
        bot.reply_to(message, "\n".join(lines_plain))


@bot.message_handler(commands=["user"])
@require_auth
def user_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        bot.reply_to(message, "ℹ️ نمونه: `/user 123456789`", parse_mode="Markdown")
        return
    target_id = parts[1].strip()
    details = get_user_details(target_id)
    if not details:
        bot.reply_to(message, f"❌ کاربر `{target_id}` یافت نشد.", parse_mode="Markdown")
        return
    response = (
        f"👤 **کاربر {target_id}:**\n"
        f"📨 کل درخواست‌ها: {details['total']}\n"
        f"✅ موفق: {details['success']}\n"
        f"🔗 آخرین لینک: {details['last_url']}\n"
        f"📅 آخرین درخواست: {format_datetime(details['created_at'])}\n"
        f"وضعیت آخرین درخواست: {details['last_status']}"
    )
    bot.reply_to(message, response, parse_mode="Markdown")


def modify_block_list(target_id: int, block: bool) -> bool:
    state = load_manager_state()
    blocked = set(state.get("blocked_users", []))
    if block:
        if target_id in blocked:
            return False
        blocked.add(target_id)
        state["blocked_users"] = sorted(blocked)
        save_manager_state(state)
        return True
    else:
        if target_id not in blocked:
            return False
        blocked.remove(target_id)
        state["blocked_users"] = sorted(blocked)
        save_manager_state(state)
        return True


@bot.message_handler(commands=["block"])
@require_auth
def block_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        bot.reply_to(message, "ℹ️ نمونه: `/block 123456789`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "❌ شناسه کاربر باید عددی باشد.")
        return
    if modify_block_list(target_id, block=True):
        bot.reply_to(message, f"🚫 کاربر `{target_id}` مسدود شد.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "ℹ️ کاربر از قبل مسدود شده بود.", parse_mode="Markdown")


@bot.message_handler(commands=["unblock"])
@require_auth
def unblock_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        bot.reply_to(message, "ℹ️ نمونه: `/unblock 123456789`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "❌ شناسه کاربر باید عددی باشد.")
        return
    if modify_block_list(target_id, block=False):
        bot.reply_to(message, f"✅ کاربر `{target_id}` آزاد شد.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "ℹ️ این کاربر در لیست مسدودها نبود.", parse_mode="Markdown")


@bot.message_handler(commands=["blocked"])
@require_auth
def blocked_cmd(message):
    state = load_manager_state()
    blocked = state.get("blocked_users", [])
    if not blocked:
        bot.reply_to(message, "✅ هیچ کاربر مسدودی وجود ندارد.")
        return
    lines = ["🚫 **کاربران مسدود:**"]
    for user_id in blocked:
        lines.append(f"- `{user_id}`")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["maintenance"])
@require_auth
def maintenance_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1 or parts[1].strip().lower() not in {"on", "off"}:
        bot.reply_to(message, "ℹ️ نمونه: `/maintenance on` یا `/maintenance off`")
        return
    turn_on = parts[1].strip().lower() == "on"
    state = load_manager_state()
    state["maintenance_mode"] = turn_on
    save_manager_state(state)
    bot.reply_to(
        message,
        "🛠️ حالت نگهداری فعال شد." if turn_on else "✅ حالت نگهداری غیرفعال شد.",
    )


def _show_status(message):
    """تابع داخلی برای نمایش وضعیت ربات دانلودر"""
    try:
        state = load_manager_state()
        bot_status = check_downloader_bot_status()
        
        status_emoji = "🟢" if bot_status.get('active') else "🔴"
        
        # نمایش ساده: فقط فعال یا غیرفعال
        if bot_status.get('active'):
            status_text = "فعال"
        else:
            status_text = "غیرفعال"
        
        required_channels = state.get('required_channels', [])
        if required_channels:
            # Escape کردن کاراکترهای خاص در نام کانال‌ها
            channel_list = []
            for ch in required_channels:
                username = ch.get('username', '').lstrip('@')
                username_safe = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
                channel_list.append(f"@{username_safe}")
            channel_text = ", ".join(channel_list)
        else:
            channel_text = "غیرفعال"
        
        # Escape کردن نام ربات
        bot_username = bot_status.get('username', 'N/A')
        bot_username_safe = bot_username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
        
        # Escape کردن یادداشت مدیر
        notes = state.get('notes') or '-'
        notes_safe = notes.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]').replace('(', '\\(').replace(')', '\\)')
        
        response = (
            "⚙️ **وضعیت فعلی ربات دانلودر**\n\n"
            f"{status_emoji} **وضعیت ربات:** {status_text}\n"
            f"👤 نام ربات: @{bot_username_safe}\n\n"
            f"🛠️ حالت نگهداری: {'فعال' if state.get('maintenance_mode') else 'غیرفعال'}\n"
            f"🔗 کانال اجباری: {channel_text}\n"
            f"🚫 کاربران مسدود: {len(state.get('blocked_users', []))}\n"
            f"📝 یادداشت مدیر: {notes_safe}"
        )
        
        try:
            bot.reply_to(message, response, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"خطا در ارسال وضعیت با Markdown: {e}")
            # ارسال بدون Markdown در صورت خطا
            response_plain = (
                "⚙️ وضعیت فعلی ربات دانلودر\n\n"
                f"{status_emoji} وضعیت ربات: {status_text}\n"
                f"👤 نام ربات: @{bot_username}\n\n"
                f"🛠️ حالت نگهداری: {'فعال' if state.get('maintenance_mode') else 'غیرفعال'}\n"
                f"🔗 کانال اجباری: {channel_text.replace('\\', '')}\n"
                f"🚫 کاربران مسدود: {len(state.get('blocked_users', []))}\n"
                f"📝 یادداشت مدیر: {notes}"
            )
            bot.reply_to(message, response_plain)
    except Exception as e:
        logger.error(f"خطا در نمایش وضعیت: {e}", exc_info=True)
        bot.reply_to(message, f"❌ خطا در دریافت وضعیت: {str(e)}")


@bot.message_handler(commands=["status"])
@require_auth
def status_cmd(message):
    """دستور /status برای نمایش وضعیت ربات دانلودر"""
    _show_status(message)


@bot.message_handler(commands=["channel"])
@require_auth
def channel_cmd(message):
    """مدیریت کانال‌های اجباری"""
    parts = message.text.split(maxsplit=2)
    state = load_manager_state()
    channels = state.get('required_channels', [])
    
    if len(parts) == 1:
        # نمایش لیست کانال‌ها
        if not channels:
            bot.reply_to(message, "ℹ️ هیچ کانال اجباری تنظیم نشده است.\n\nبرای افزودن: `/channel add @username عنوان`\nبرای حذف: `/channel remove @username`", parse_mode="Markdown")
        else:
            lines = ["🔗 **کانال‌های اجباری:**\n"]
            for idx, ch in enumerate(channels, 1):
                username = ch.get('username', 'N/A')
                title = ch.get('title', 'بدون عنوان')
                lines.append(f"{idx}. @{username.lstrip('@')} - {title}")
            lines.append("\n💡 برای افزودن: `/channel add @username عنوان`")
            lines.append("💡 برای حذف: `/channel remove @username`")
            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
        return
    
    action = parts[1].strip().lower()
    
    if action == 'add':
        if len(parts) < 3:
            bot.reply_to(message, "ℹ️ نمونه: `/channel add @username عنوان کانال`", parse_mode="Markdown")
            return
        channel_input = parts[2].strip()
        # جدا کردن username و title
        if ' ' in channel_input:
            username, title = channel_input.split(' ', 1)
        else:
            username = channel_input
            title = "کانال اجباری"
        
        # حذف @ از ابتدا اگر وجود دارد
        username = username.lstrip('@')
        
        # بررسی تکراری نبودن
        if any(ch.get('username', '').lstrip('@') == username for ch in channels):
            bot.reply_to(message, f"⚠️ کانال @{username} قبلاً اضافه شده است.")
            return
        
        channels.append({"username": f"@{username}", "title": title})
        state["required_channels"] = channels
        save_manager_state(state)
        bot.reply_to(message, f"✅ کانال اضافه شد: @{username} - {title}", parse_mode="Markdown")
    
    elif action == 'remove':
        if len(parts) < 3:
            bot.reply_to(message, "ℹ️ نمونه: `/channel remove @username`", parse_mode="Markdown")
            return
        channel_input = parts[2].strip().lstrip('@')
        
        # حذف کانال
        original_count = len(channels)
        channels = [ch for ch in channels if ch.get('username', '').lstrip('@') != channel_input]
        
        if len(channels) < original_count:
            state["required_channels"] = channels
            save_manager_state(state)
            bot.reply_to(message, f"✅ کانال @{channel_input} حذف شد.", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"⚠️ کانال @{channel_input} یافت نشد.", parse_mode="Markdown")
    
    elif action == 'clear':
        state["required_channels"] = []
        save_manager_state(state)
        bot.reply_to(message, "✅ تمام کانال‌های اجباری حذف شدند.")
    
    else:
        bot.reply_to(message, "ℹ️ دستورات:\n• `/channel add @username عنوان`\n• `/channel remove @username`\n• `/channel clear`", parse_mode="Markdown")


@bot.message_handler(commands=["note"])
@require_auth
def note_cmd(message):
    parts = message.text.split(maxsplit=1)
    note_text = parts[1].strip() if len(parts) > 1 else ""
    state = load_manager_state()
    state["notes"] = note_text
    save_manager_state(state)
    bot.reply_to(message, "📝 یادداشت مدیریتی به‌روزرسانی شد.")


@bot.message_handler(func=lambda msg: True)
def fallback_handler(message):
    if not is_authenticated(message.from_user.id):
        # بررسی اینکه آیا پیام رمز ورود است
        text = message.text if message.text else ""
        if check_password(text):
            refresh_session(message.from_user.id)
            bot.reply_to(
                message,
                "✅ **خوش آمدید!**\n\n🔓 دسترسی مدیریتی برای شما باز شد.\n\n💡 از منوی زیر یا دستورات برای مدیریت ربات استفاده کنید.",
                parse_mode="Markdown",
                reply_markup=build_main_menu(),
            )
            return
        bot.reply_to(message, "🔐 لطفاً ابتدا وارد شوید: `/login <password>`\n\n💡 یا رمز عبور را مستقیماً ارسال کنید.", parse_mode="Markdown")
        return
    text = message.text.strip() if message.text else ""
    if text == "📊 آمار کلی":
        stats_cmd(message)
    elif text == "👤 لیست تمام کاربران":
        users_cmd(message)
    elif text == "🚫 مسدود کردن":
        bot.reply_to(message, "برای مسدود کردن از `/block <user_id>` استفاده کنید.")
    elif text == "✅ رفع انسداد":
        bot.reply_to(message, "برای رفع انسداد از `/unblock <user_id>` استفاده کنید.")
    elif text == "🔐 ورود مجدد":
        bot.reply_to(message, "برای تمدید ورود، دوباره `/login <password>` را ارسال کنید.")
    elif text == "⚙️ وضعیت ربات دانلودر":
        _show_status(message)
    elif text == "🔗 مدیریت کانال‌ها":
        # نمایش منوی اینلاین برای مدیریت کانال‌ها
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("📋 نمایش لیست کانال‌ها", callback_data="channels_list"))
        markup.add(types.InlineKeyboardButton("➕ افزودن کانال", callback_data="channel_add"))
        markup.add(types.InlineKeyboardButton("➖ حذف کانال", callback_data="channel_remove"))
        markup.add(types.InlineKeyboardButton("🗑️ پاک کردن همه", callback_data="channel_clear"))
        markup.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_menu"))
        
        state = load_manager_state()
        channels = state.get('required_channels', [])
        count = len(channels)
        
        message_text = (
            f"🔗 **مدیریت کانال‌های اجباری**\n\n"
            f"📊 تعداد کانال‌های فعلی: {count}\n\n"
            f"💡 از دکمه‌های زیر برای مدیریت استفاده کنید:"
        )
        bot.reply_to(message, message_text, parse_mode="Markdown", reply_markup=markup)
    elif message.from_user.id in USER_STATES:
        # اگر کاربر در حال افزودن یا حذف کانال است
        handle_channel_state(message)
    else:
        help_cmd(message)


def show_channels_menu(message):
    """نمایش منوی مدیریت کانال‌ها"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📋 نمایش لیست کانال‌ها", callback_data="channels_list"))
    markup.add(types.InlineKeyboardButton("➕ افزودن کانال", callback_data="channel_add"))
    markup.add(types.InlineKeyboardButton("➖ حذف کانال", callback_data="channel_remove"))
    markup.add(types.InlineKeyboardButton("🗑️ پاک کردن همه", callback_data="channel_clear"))
    
    state = load_manager_state()
    channels = state.get('required_channels', [])
    count = len(channels)
    
    message_text = (
        f"🔗 **مدیریت کانال‌های اجباری**\n\n"
        f"📊 تعداد کانال‌های فعلی: {count}\n\n"
        f"💡 از دکمه‌های زیر برای مدیریت استفاده کنید:"
    )
    bot.reply_to(message, message_text, parse_mode="Markdown", reply_markup=markup)


def handle_channel_state(message):
    """مدیریت وضعیت کاربر در حال افزودن یا حذف کانال"""
    user_id = message.from_user.id
    state_type = USER_STATES.get(user_id)
    text = message.text.strip() if message.text else ""
    
    if state_type == "adding_channel":
        # افزودن کانال
        parts = text.split(maxsplit=1)
        if not parts:
            bot.reply_to(message, "❌ لطفاً username کانال را وارد کنید (مثلاً: @channelname یا channelname)")
            return
        
        username = parts[0].lstrip('@')
        title = parts[1] if len(parts) > 1 else f"کانال {username}"
        
        manager_state = load_manager_state()
        channels = manager_state.get('required_channels', [])
        
        # بررسی تکراری نبودن
        if any(ch.get('username', '').lstrip('@') == username for ch in channels):
            bot.reply_to(message, f"⚠️ کانال @{username} قبلاً اضافه شده است.")
            USER_STATES.pop(user_id, None)
            return
        
        channels.append({"username": f"@{username}", "title": title})
        manager_state["required_channels"] = channels
        save_manager_state(manager_state)
        USER_STATES.pop(user_id, None)
        bot.reply_to(message, f"✅ کانال اضافه شد:\n\n📢 عنوان: {title}\n🔗 @{username}", parse_mode="Markdown")
    
    elif state_type == "removing_channel":
        # حذف کانال
        username = text.lstrip('@')
        manager_state = load_manager_state()
        channels = manager_state.get('required_channels', [])
        
        original_count = len(channels)
        channels = [ch for ch in channels if ch.get('username', '').lstrip('@') != username]
        
        if len(channels) < original_count:
            manager_state["required_channels"] = channels
            save_manager_state(manager_state)
            USER_STATES.pop(user_id, None)
            bot.reply_to(message, f"✅ کانال @{username} حذف شد.", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"⚠️ کانال @{username} یافت نشد.")
            USER_STATES.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """مدیریت callback های دکمه‌های اینلاین"""
    if not is_authenticated(call.from_user.id):
        bot.answer_callback_query(call.id, "🔐 لطفاً ابتدا وارد شوید.")
        return
    
    try:
        if call.data == "channels_list":
            state = load_manager_state()
            channels = state.get('required_channels', [])
            
            if not channels:
                bot.answer_callback_query(call.id, "ℹ️ هیچ کانالی تنظیم نشده است.")
                try:
                    bot.edit_message_text(
                        "ℹ️ **لیست کانال‌های اجباری**\n\n❌ هیچ کانال اجباری تنظیم نشده است.",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode="Markdown",
                        reply_markup=create_channels_menu_markup()
                    )
                except:
                    pass
            else:
                lines = ["📋 **لیست کانال‌های اجباری:**\n"]
                for idx, ch in enumerate(channels, 1):
                    username = ch.get('username', 'N/A').lstrip('@')
                    title = ch.get('title', 'بدون عنوان')
                    lines.append(f"{idx}. {title}\n   🔗 @{username}")
                
                bot.answer_callback_query(call.id)
                try:
                    bot.edit_message_text(
                        "\n".join(lines),
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode="Markdown",
                        reply_markup=create_channels_menu_markup()
                    )
                except:
                    pass
        
        elif call.data == "channel_add":
            USER_STATES[call.from_user.id] = "adding_channel"
            bot.answer_callback_query(call.id, "➕ لطفاً username کانال را ارسال کنید")
            try:
                bot.edit_message_text(
                    "➕ **افزودن کانال جدید**\n\n"
                    "💡 لطفاً username کانال را به این صورت ارسال کنید:\n"
                    "• `@channelname عنوان کانال`\n"
                    "• یا فقط `@channelname`\n\n"
                    "مثال: `@mychannel کانال من`",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown",
                    reply_markup=create_channels_menu_markup()
                )
            except:
                pass
        
        elif call.data == "channel_remove":
            USER_STATES[call.from_user.id] = "removing_channel"
            state = load_manager_state()
            channels = state.get('required_channels', [])
            
            if not channels:
                bot.answer_callback_query(call.id, "⚠️ هیچ کانالی برای حذف وجود ندارد.")
                USER_STATES.pop(call.from_user.id, None)
                return
            
            # ساخت دکمه‌های اینلاین برای انتخاب کانال
            markup = types.InlineKeyboardMarkup(row_width=1)
            for ch in channels:
                username = ch.get('username', '').lstrip('@')
                title = ch.get('title', username)
                markup.add(types.InlineKeyboardButton(
                    f"🗑️ {title} (@{username})",
                    callback_data=f"remove_channel_{username}"
                ))
            markup.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_channels_menu"))
            
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text(
                    "➖ **حذف کانال**\n\n💡 کانال مورد نظر را انتخاب کنید:",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
            except:
                pass
        
        elif call.data.startswith("remove_channel_"):
            username = call.data.replace("remove_channel_", "")
            state = load_manager_state()
            channels = state.get('required_channels', [])
            
            original_count = len(channels)
            channels = [ch for ch in channels if ch.get('username', '').lstrip('@') != username]
            
            if len(channels) < original_count:
                state["required_channels"] = channels
                save_manager_state(state)
                bot.answer_callback_query(call.id, f"✅ کانال @{username} حذف شد.")
                try:
                    bot.edit_message_text(
                        f"✅ **کانال حذف شد**\n\n🔗 @{username}",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode="Markdown",
                        reply_markup=create_channels_menu_markup()
                    )
                except:
                    pass
            else:
                bot.answer_callback_query(call.id, "⚠️ کانال یافت نشد.")
        
        elif call.data == "channel_clear":
            state = load_manager_state()
            channels = state.get('required_channels', [])
            
            if not channels:
                bot.answer_callback_query(call.id, "⚠️ هیچ کانالی برای حذف وجود ندارد.")
                return
            
            # تأیید حذف
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("✅ بله، حذف کن", callback_data="confirm_clear_channels"),
                types.InlineKeyboardButton("❌ انصراف", callback_data="back_to_channels_menu")
            )
            
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text(
                    f"🗑️ **پاک کردن همه کانال‌ها**\n\n⚠️ آیا مطمئن هستید که می‌خواهید {len(channels)} کانال را حذف کنید؟",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
            except:
                pass
        
        elif call.data == "confirm_clear_channels":
            state = load_manager_state()
            state["required_channels"] = []
            save_manager_state(state)
            bot.answer_callback_query(call.id, "✅ تمام کانال‌ها حذف شدند.")
            try:
                bot.edit_message_text(
                    "✅ **تمام کانال‌های اجباری حذف شدند.**",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown",
                    reply_markup=create_channels_menu_markup()
                )
            except:
                pass
        
        elif call.data == "back_to_channels_menu":
            show_channels_menu_edit(call.message)
        
    except Exception as e:
        logger.error(f"خطا در callback handler: {e}")
        bot.answer_callback_query(call.id, "❌ خطایی رخ داد.")


def create_channels_menu_markup():
    """ایجاد منوی مدیریت کانال‌ها"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📋 نمایش لیست کانال‌ها", callback_data="channels_list"))
    markup.add(types.InlineKeyboardButton("➕ افزودن کانال", callback_data="channel_add"))
    markup.add(types.InlineKeyboardButton("➖ حذف کانال", callback_data="channel_remove"))
    markup.add(types.InlineKeyboardButton("🗑️ پاک کردن همه", callback_data="channel_clear"))
    return markup


def show_channels_menu_edit(message):
    """ویرایش پیام برای نمایش منوی کانال‌ها"""
    state = load_manager_state()
    channels = state.get('required_channels', [])
    count = len(channels)
    
    message_text = (
        f"🔗 **مدیریت کانال‌های اجباری**\n\n"
        f"📊 تعداد کانال‌های فعلی: {count}\n\n"
        f"💡 از دکمه‌های زیر برای مدیریت استفاده کنید:"
    )
    try:
        bot.edit_message_text(
            message_text,
            message.chat.id,
            message.message_id,
            parse_mode="Markdown",
            reply_markup=create_channels_menu_markup()
        )
    except:
        pass


def setup_bot_commands():
    """تنظیم منوی همبرگری (Bot Commands)"""
    try:
        commands = [
            types.BotCommand("start", "شروع کار با ربات مدیریت"),
            types.BotCommand("help", "راهنمای دستورات"),
            types.BotCommand("status", "وضعیت ربات دانلودر"),
            types.BotCommand("stats", "آمار کلی ربات دانلودر"),
            types.BotCommand("users", "لیست تمام کاربران"),
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
                {"command": "start", "description": "شروع کار با ربات مدیریت"},
                {"command": "help", "description": "راهنمای دستورات"},
                {"command": "status", "description": "وضعیت ربات دانلودر"},
                {"command": "stats", "description": "آمار کلی ربات دانلودر"},
                {"command": "users", "description": "لیست تمام کاربران"},
            ]
            apihelper.set_my_commands(bot.token, commands)
            logger.info("✅ منوی همبرگری با apihelper تنظیم شد")
        except Exception as e:
            logger.error(f"خطا در تنظیم منوی همبرگری (apihelper): {e}")
    except Exception as e:
        logger.error(f"خطا در تنظیم منوی همبرگری: {e}")
        # تلاش با استفاده مستقیم از API
        try:
            url = f"https://api.telegram.org/bot{bot.token}/setMyCommands"
            commands_data = {
                "commands": [
                    {"command": "start", "description": "شروع کار با ربات مدیریت"},
                    {"command": "help", "description": "راهنمای دستورات"},
                    {"command": "status", "description": "وضعیت ربات دانلودر"},
                    {"command": "stats", "description": "آمار کلی ربات دانلودر"},
                    {"command": "users", "description": "لیست تمام کاربران"},
                ]
            }
            response = requests.post(url, json=commands_data, timeout=10)
            if response.status_code == 200:
                logger.info("✅ منوی همبرگری با API مستقیم تنظیم شد")
            else:
                logger.error(f"خطا در تنظیم منوی همبرگری (API): {response.status_code}")
        except Exception as e2:
            logger.error(f"خطا در تنظیم منوی همبرگری (API مستقیم): {e2}")


def main():
    logger.info("🚀 ربات مدیریت آماده اجرا است.")
    logger.info(f"🔐 رمز ورود تنظیم شده: '{ADMIN_PASSWORD}' (طول: {len(ADMIN_PASSWORD)})")
    
    # تنظیم منوی همبرگری
    logger.info("📱 در حال تنظیم منوی همبرگری...")
    setup_bot_commands()
    
    # بررسی اینکه آیا منو تنظیم شد
    try:
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
    
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception as exc:
            logger.error("خطا در polling: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()

