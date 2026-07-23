from __future__ import annotations

import logging
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from accounts import account_manager
from campaign import campaign_engine
from config import ADMIN_IDS, LOGS_DIR, SESSIONS_DIR
from logger_setup import log_buffer
from monitor import monitor_service
from storage import storage

logger = logging.getLogger(__name__)
router = Router()

USERNAME_RE = re.compile(r"@?([A-Za-z0-9_]{4,})")


class Form(StatesGroup):
    waiting_targets = State()
    waiting_session = State()
    waiting_tdata = State()
    waiting_phone = State()
    waiting_phone_code = State()
    waiting_2fa = State()
    waiting_remove_account = State()
    waiting_remove_target = State()
    waiting_phrases_ru = State()
    waiting_phrases_en = State()


def is_admin(user_id: int | None) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id is not None and user_id in ADMIN_IDS


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📜 Логи")],
            [KeyboardButton(text="👤 Аккаунты"), KeyboardButton(text="🎯 Целевые боты")],
            [KeyboardButton(text="▶ Старт кампании"), KeyboardButton(text="⏹ Стоп")],
            [KeyboardButton(text="🔍 Проверить ботов"), KeyboardButton(text="📈 Удалено за 3 дня")],
            [KeyboardButton(text="📝 Тексты жалоб"), KeyboardButton(text="🔄 Обновить аккаунты")],
        ],
        resize_keyboard=True,
    )


def accounts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить .session", callback_data="acc:add")],
            [InlineKeyboardButton(text="📦 Добавить tdata (zip)", callback_data="acc:tdata")],
            [InlineKeyboardButton(text="📱 Добавить по номеру", callback_data="acc:phone")],
            [InlineKeyboardButton(text="📋 Список", callback_data="acc:list")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="acc:del")],
            [InlineKeyboardButton(text="🔄 Проверить все", callback_data="acc:refresh")],
        ]
    )


def targets_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить ботов", callback_data="tgt:add")],
        [InlineKeyboardButton(text="📋 Список / выбор", callback_data="tgt:list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="tgt:del")],
        [InlineKeyboardButton(text="✅ Выбрать все", callback_data="tgt:all_on")],
        [InlineKeyboardButton(text="⬜ Снять все", callback_data="tgt:all_off")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def texts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Фразы RU", callback_data="txt:phrases_ru")],
            [InlineKeyboardButton(text="Фразы EN", callback_data="txt:phrases_en")],
            [InlineKeyboardButton(text="Загрузить фразы RU", callback_data="txt:up_ru")],
            [InlineKeyboardButton(text="Загрузить фразы EN", callback_data="txt:up_en")],
        ]
    )


def language_kb() -> InlineKeyboardMarkup:
    from campaign import load_phrases
    from config import LANGUAGE_LABELS

    rows = []
    for code, label in LANGUAGE_LABELS.items():
        n = len(load_phrases(code))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} ({n} фраз)",
                    callback_data=f"camp:lang:{code}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="camp:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_type_kb(lang: str) -> InlineKeyboardMarkup:
    from config import REPORT_CATEGORIES, REPORT_TYPE_CHOICES

    by_id = {c["id"]: c for c in REPORT_CATEGORIES}
    rows = []
    for cat_id in REPORT_TYPE_CHOICES:
        cat = by_id[cat_id]
        title = cat.get("short_title") or cat["title"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=f"camp:type:{lang}:{cat_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="camp:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _report_category_label(category_id: str | None) -> str:
    from config import get_report_category

    cat = get_report_category(category_id)
    return cat.get("short_title") or cat["title"]


async def send_long(message: Message, text: str) -> None:
    chunk = 3500
    for i in range(0, len(text), chunk):
        await message.answer(text[i : i + chunk])


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await message.answer(
        "Жалобщик-бот готов.\n"
        "Аккаунты: Telethon .session\n"
        "Управление — через меню ниже.",
        reply_markup=main_menu(),
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    aid = data.get("login_account_id")
    if aid:
        await account_manager.cancel_phone_login(aid)
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


@router.message(F.text == "📊 Статус")
async def status_handler(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    accounts = storage.list_accounts()
    targets = storage.list_targets()
    camp = storage.campaign()
    selected = storage.selected_targets()
    recent = storage.deleted_last_days(3)

    by_status: dict[str, int] = {}
    spam = ok = dead = 0
    for acc in accounts.values():
        st = acc.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        if acc.get("spamblock") is True:
            spam += 1
        elif acc.get("spamblock") is False:
            ok += 1
        if st == "dead":
            dead += 1

    lines = [
        "=== СТАТУС ===",
        f"Кампания: {camp.get('status')} | раунд {camp.get('current_round')}/{camp.get('total_rounds')}",
        f"Язык фраз: {camp.get('report_language') or storage.get_report_language()}",
        f"Вид жалобы: {_report_category_label(camp.get('report_category') or storage.get_report_category())}",
        f"Активна: {'да' if camp.get('active') else 'нет'}",
        f"Старт: {camp.get('started_at') or '—'}",
        f"Финиш: {camp.get('finished_at') or '—'}",
        "",
        f"Аккаунтов: {len(accounts)} | мёртвых: {dead}",
        f"Спамблок: да={spam}, нет={ok}, ?={len(accounts) - spam - ok}",
        "По статусам: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "—",
        "",
        f"Целевых ботов: {len(targets)} | выбрано: {len(selected)}",
        f"Удалено за 3 суток: {len(recent)}",
    ]
    if selected:
        lines.append("Выбраны: " + ", ".join(f"@{u}" for u in selected[:40]))
    if camp.get("last_error"):
        lines.append(f"Ошибка: {camp['last_error']}")

    lines.append("")
    lines.append("--- Аккаунты ---")
    if not accounts:
        lines.append("нет")
    for aid, acc in list(accounts.items())[:40]:
        sb = acc.get("spamblock")
        sb_s = "SB+" if sb is True else ("SB-" if sb is False else "SB?")
        lines.append(
            f"• {aid} | {acc.get('status')} | {sb_s} | @{acc.get('username') or '—'} | {acc.get('phone') or '—'}"
        )

    lines.append("")
    lines.append("--- Боты ---")
    if not targets:
        lines.append("нет")
    for uname, t in list(targets.items())[:50]:
        mark = "✓" if t.get("selected") else "·"
        lines.append(f"{mark} @{uname} | {t.get('status')} | last={t.get('last_reported_at') or '—'}")

    await send_long(message, "\n".join(lines))


@router.message(F.text == "📜 Логи")
async def logs_handler(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    text = log_buffer.tail(40)
    await send_long(message, f"<code>{_escape(text)}</code>")
    log_path = LOGS_DIR / "bot.log"
    if log_path.exists() and log_path.stat().st_size > 0:
        await message.answer_document(FSInputFile(log_path), caption="Полный лог-файл")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@router.message(F.text == "👤 Аккаунты")
async def accounts_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Управление аккаунтами (.session):", reply_markup=accounts_kb())


@router.message(F.text == "🎯 Целевые боты")
async def targets_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Целевые боты для жалоб:", reply_markup=targets_kb())


@router.message(F.text == "📝 Тексты жалоб")
async def texts_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Фразы для комментария к жалобе (по языкам):",
        reply_markup=texts_kb(),
    )


@router.message(F.text == "▶ Старт кампании")
async def start_campaign(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if campaign_engine.running:
        await message.answer("Кампания уже запущена.")
        return
    if not storage.selected_targets():
        await message.answer("Сначала добавьте и выберите целевых ботов.")
        return
    if not account_manager.campaign_accounts():
        await message.answer("Нет доступных аккаунтов.")
        return
    await message.answer(
        "Выберите язык фраз для жалоб:",
        reply_markup=language_kb(),
    )


@router.callback_query(F.data == "camp:cancel")
async def cb_camp_cancel(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.edit_text("Запуск отменён.")
    await call.answer()


@router.callback_query(F.data.startswith("camp:lang:"))
async def cb_camp_lang(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    code = call.data.split(":")[-1]
    from config import LANGUAGE_LABELS, PHRASE_FILES

    if code not in PHRASE_FILES:
        await call.answer("Неизвестный язык", show_alert=True)
        return
    await call.answer()
    label = LANGUAGE_LABELS.get(code, code)
    await call.message.edit_text(
        f"Язык: {label}\n\nВыберите вид жалобы:",
        reply_markup=report_type_kb(code),
    )


@router.callback_query(F.data.startswith("camp:type:"))
async def cb_camp_type(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    parts = call.data.split(":")
    # camp:type:{lang}:{category_id}
    if len(parts) != 4:
        await call.answer("Некорректные данные", show_alert=True)
        return
    lang, category_id = parts[2], parts[3]
    from config import PHRASE_FILES, REPORT_TYPE_CHOICES

    if lang not in PHRASE_FILES or category_id not in REPORT_TYPE_CHOICES:
        await call.answer("Неизвестный параметр", show_alert=True)
        return
    await call.answer("Запускаю…")
    result = await campaign_engine.start(language=lang, category_id=category_id)
    try:
        await call.message.edit_text(result)
    except Exception:
        await call.message.answer(result)


@router.message(F.text == "⏹ Стоп")
async def stop_campaign(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    result = await campaign_engine.stop()
    await message.answer(result)


@router.message(F.text == "🔍 Проверить ботов")
async def check_bots(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Проверяю целевых ботов…")
    report = await monitor_service.check_targets(reason="manual")
    await send_long(message, report)


@router.message(F.text == "📈 Удалено за 3 дня")
async def deleted_stats(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    items = storage.deleted_last_days(3)
    if not items:
        await message.answer("За 3 суток удалений не зафиксировано.")
        return
    lines = [f"Удалено за 3 суток: {len(items)}"]
    for it in items[-50:]:
        lines.append(f"• @{it['username']} — {it['detected_at']}")
    await send_long(message, "\n".join(lines))


@router.message(F.text == "🔄 Обновить аккаунты")
async def refresh_accounts(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Проверяю аккаунты (SpamBot)…")
    results = await account_manager.refresh_all()
    lines = [f"Проверено: {len(results)}"]
    for acc in results:
        sb = acc.get("spamblock")
        sb_s = "спамблок" if sb is True else ("ок" if sb is False else "?")
        lines.append(f"• {acc['id']}: {acc.get('status')} / {sb_s}")
    alive = len(account_manager.alive_accounts())
    lines.append(f"Доступно: {alive}")
    await send_long(message, "\n".join(lines))


# ---- callbacks accounts ----
@router.callback_query(F.data == "acc:add")
async def cb_acc_add(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Form.waiting_session)
    await call.message.answer(
        "Пришлите файл .session (Telethon).\n"
        "Можно несколько файлов подряд.\n"
        "/cancel — отмена"
    )
    await call.answer()


@router.callback_query(F.data == "acc:tdata")
async def cb_acc_tdata(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Form.waiting_tdata)
    await call.message.answer(
        "Добавление через <b>tdata</b>:\n"
        "1. Закройте Telegram Desktop\n"
        "2. Заархивируйте папку <code>tdata</code> в <b>.zip</b>\n"
        "3. Пришлите zip сюда\n\n"
        "Если включён локальный passcode Desktop — напишите его "
        "в подписи к файлу.\n"
        "⚠️ Не держите Desktop онлайн с тем же аккаунтом после импорта "
        "(иначе сессию убьёт по двум IP).\n"
        "/cancel — отмена"
    )
    await call.answer()


@router.callback_query(F.data == "acc:phone")
async def cb_acc_phone(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Form.waiting_phone)
    await call.message.answer(
        "Введите номер телефона аккаунта в международном формате:\n"
        "Пример: <code>+12025550123</code>\n"
        "/cancel — отмена"
    )
    await call.answer()


@router.callback_query(F.data == "acc:list")
async def cb_acc_list(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    accounts = storage.list_accounts()
    if not accounts:
        await call.message.answer("Аккаунтов нет.")
    else:
        lines = ["Аккаунты:"]
        for aid, acc in accounts.items():
            lines.append(
                f"• <b>{aid}</b> | {acc.get('status')} | spam={acc.get('spamblock')} | "
                f"@{acc.get('username') or '—'} | err={acc.get('last_error') or '—'}"
            )
        await send_long(call.message, "\n".join(lines))
    await call.answer()


@router.callback_query(F.data == "acc:del")
async def cb_acc_del(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    accounts = list(storage.list_accounts())
    if not accounts:
        await call.message.answer("Нечего удалять.")
        await call.answer()
        return
    await state.set_state(Form.waiting_remove_account)
    await call.message.answer(
        "Введите id аккаунта для удаления:\n" + ", ".join(accounts) + "\n/cancel — отмена"
    )
    await call.answer()


@router.callback_query(F.data == "acc:refresh")
async def cb_acc_refresh(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer("Проверяю…")
    await call.message.answer("Обновляю статусы аккаунтов…")
    results = await account_manager.refresh_all()
    await call.message.answer(f"Готово. Проверено: {len(results)}")


@router.message(Form.waiting_session, F.document)
async def on_session_file(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    doc = message.document
    name = doc.file_name or "account.session"
    if not name.endswith(".session"):
        await message.answer("Нужен файл с расширением .session")
        return

    from io import BytesIO

    buffer = BytesIO()
    await message.bot.download(doc, destination=buffer)
    data = buffer.getvalue()

    tmp = SESSIONS_DIR / f"_upload_{name}"
    tmp.write_bytes(data)

    base = Path(name).stem

    try:
        aid = await account_manager.add_session_file(tmp, account_id=base)
        await message.answer(
            f"Аккаунт добавлен: <b>{aid}</b>\nСтатус обновлён.\nМожно прислать ещё файл или /cancel",
            reply_markup=main_menu(),
        )
    except Exception as e:
        logger.exception("add session failed")
        await message.answer(f"Ошибка добавления: {e}")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    # stay in state to allow more uploads


@router.message(Form.waiting_session)
async def on_session_wrong(message: Message) -> None:
    await message.answer("Пришлите документ .session или /cancel")


@router.message(Form.waiting_tdata, F.document)
async def on_tdata_zip(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    doc = message.document
    name = (doc.file_name or "tdata.zip").lower()
    if not name.endswith(".zip"):
        await message.answer("Нужен архив <b>.zip</b> с папкой tdata.")
        return

    from io import BytesIO
    import tempfile

    from tdata_util import cleanup_dir, extract_tdata_zip

    await message.answer("Принял архив, конвертирую tdata…")

    buffer = BytesIO()
    await message.bot.download(doc, destination=buffer)
    data = buffer.getvalue()
    if not data:
        await message.answer("Пустой файл.")
        return

    passcode = (message.caption or "").strip() or None
    work = Path(tempfile.mkdtemp(prefix="tdata_bot_"))
    zip_path = work / "upload.zip"
    extract_dir = work / "extracted"
    try:
        zip_path.write_bytes(data)
        tdata_path = extract_tdata_zip(zip_path, extract_dir)
        results = await account_manager.add_from_tdata(tdata_path, passcode=passcode)
        await state.clear()

        lines = ["Импорт tdata:"]
        for r in results:
            if r.get("status") == "ok":
                uname = f"@{r['username']}" if r.get("username") else "—"
                lines.append(
                    f"✅ <b>{r['account_id']}</b> | {uname} | spam={r.get('spamblock')}"
                )
            elif r.get("status") == "skipped":
                lines.append(f"⏭ пропуск: {r.get('error')}")
            else:
                lines.append(f"❌ [{r.get('index')}] {r.get('error')}")
        lines.append("\nМожно добавить ещё через меню Аккаунты.")
        await send_long(message, "\n".join(lines))
        await message.answer("Готово.", reply_markup=main_menu())
    except Exception as e:
        logger.exception("tdata import failed")
        await message.answer(f"Ошибка импорта tdata:\n{e}")
    finally:
        cleanup_dir(work)


@router.message(Form.waiting_tdata)
async def on_tdata_wrong(message: Message) -> None:
    await message.answer("Пришлите .zip с папкой tdata или /cancel")


@router.message(Form.waiting_phone)
async def on_phone_number(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        info = await account_manager.start_phone_login(message.text or "")
    except ValueError as e:
        await message.answer(f"{e}\nПопробуйте ещё раз или /cancel")
        return
    except Exception as e:
        logger.exception("phone login start failed")
        await message.answer(f"Ошибка: {e}")
        await state.clear()
        return

    await state.set_state(Form.waiting_phone_code)
    await state.update_data(
        login_account_id=info["account_id"],
        login_phone=info["phone"],
        phone_code_hash=info["phone_code_hash"],
    )
    await message.answer(
        f"Код отправлен на <code>{info['phone']}</code>.\n"
        f"ID аккаунта: <b>{info['account_id']}</b>\n"
        "Пришлите код из Telegram/SMS.\n/cancel — отмена"
    )


@router.message(Form.waiting_phone_code)
async def on_phone_code(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    try:
        result = await account_manager.confirm_phone_code(
            account_id=data["login_account_id"],
            phone=data["login_phone"],
            phone_code_hash=data["phone_code_hash"],
            code=message.text or "",
        )
    except ValueError as e:
        await message.answer(f"{e}")
        return
    except Exception as e:
        logger.exception("phone code failed")
        await account_manager.cancel_phone_login(data.get("login_account_id"))
        await state.clear()
        await message.answer(f"Ошибка: {e}", reply_markup=main_menu())
        return

    if result.get("status") == "2fa":
        await state.set_state(Form.waiting_2fa)
        await message.answer("Нужен облачный пароль 2FA. Пришлите пароль.\n/cancel — отмена")
        return

    await state.clear()
    info = result.get("info") or {}
    sb = info.get("spamblock")
    sb_s = "спамблок" if sb is True else ("ок" if sb is False else "?")
    await message.answer(
        f"Аккаунт добавлен: <b>{result['account_id']}</b>\n"
        f"Статус: {info.get('status')} | {sb_s}\n"
        f"@{info.get('username') or '—'} | {info.get('phone') or '—'}",
        reply_markup=main_menu(),
    )


@router.message(Form.waiting_2fa)
async def on_2fa(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    try:
        result = await account_manager.confirm_2fa(
            account_id=data["login_account_id"],
            password=message.text or "",
        )
    except ValueError as e:
        await message.answer(f"{e}")
        return
    except Exception as e:
        logger.exception("2fa failed")
        await account_manager.cancel_phone_login(data.get("login_account_id"))
        await state.clear()
        await message.answer(f"Ошибка: {e}", reply_markup=main_menu())
        return

    await state.clear()
    info = result.get("info") or {}
    await message.answer(
        f"Аккаунт добавлен (2FA): <b>{result['account_id']}</b>\n"
        f"Статус: {info.get('status')}",
        reply_markup=main_menu(),
    )


@router.message(Form.waiting_remove_account)
async def on_remove_account(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    aid = (message.text or "").strip()
    ok = await account_manager.remove_account(aid)
    await state.clear()
    if ok:
        await message.answer(f"Аккаунт {aid} удалён.", reply_markup=main_menu())
    else:
        await message.answer("Аккаунт не найден.", reply_markup=main_menu())


# ---- targets ----
@router.callback_query(F.data == "tgt:add")
async def cb_tgt_add(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Form.waiting_targets)
    await call.message.answer(
        "Пришлите список ботов (через пробел, запятую или с новой строки):\n"
        "@bot1\nbot2\n@bot3\n\n/cancel — отмена"
    )
    await call.answer()


@router.callback_query(F.data == "tgt:list")
async def cb_tgt_list(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    targets = storage.list_targets()
    if not targets:
        await call.message.answer("Список пуст.")
        await call.answer()
        return

    rows = []
    for uname, t in targets.items():
        mark = "✅" if t.get("selected") else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} @{uname} [{t.get('status')}]",
                    callback_data=f"tgt:tog:{uname}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="tgt:close")])
    await call.message.answer(
        "Нажмите чтобы выбрать/снять бота для жалоб:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("tgt:tog:"))
async def cb_tgt_toggle(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    uname = call.data.split(":", 2)[2]
    t = storage.list_targets().get(uname)
    if not t:
        await call.answer("Не найден", show_alert=True)
        return
    new_val = not t.get("selected", False)
    await storage.set_target_selected(uname, new_val)
    await call.answer(f"@{uname}: {'выбран' if new_val else 'снят'}")
    # refresh list
    targets = storage.list_targets()
    rows = []
    for u, tt in targets.items():
        mark = "✅" if tt.get("selected") else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} @{u} [{tt.get('status')}]",
                    callback_data=f"tgt:tog:{u}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="tgt:close")])
    try:
        await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass


@router.callback_query(F.data == "tgt:close")
async def cb_tgt_close(call: CallbackQuery) -> None:
    await call.message.edit_text("Список закрыт.")
    await call.answer()


@router.callback_query(F.data == "tgt:del")
async def cb_tgt_del(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Form.waiting_remove_target)
    names = ", ".join(f"@{u}" for u in storage.list_targets())
    await call.message.answer(f"Введите username бота для удаления:\n{names}\n/cancel")
    await call.answer()


@router.callback_query(F.data == "tgt:all_on")
async def cb_tgt_all_on(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    for uname in storage.list_targets():
        await storage.set_target_selected(uname, True)
    await call.message.answer("Все боты выбраны.")
    await call.answer()


@router.callback_query(F.data == "tgt:all_off")
async def cb_tgt_all_off(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    for uname in storage.list_targets():
        await storage.set_target_selected(uname, False)
    await call.message.answer("Выбор снят со всех.")
    await call.answer()


@router.message(Form.waiting_targets)
async def on_targets_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    text = message.text or ""
    found = USERNAME_RE.findall(text)
    # filter noise
    skip = {"http", "https", "t", "me", "telegram"}
    usernames = []
    for u in found:
        if u.lower() in skip:
            continue
        if len(u) < 4:
            continue
        usernames.append(u.lower())
    # unique preserve order
    seen = set()
    uniq = []
    for u in usernames:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    if not uniq:
        await message.answer("Не нашёл username. Пример: @mybot bot2")
        return

    current = len(storage.list_targets())
    added = []
    for u in uniq:
        if current + len(added) >= 20:
            break
        await storage.upsert_target(u, {"selected": True, "status": "active"})
        added.append(u)

    await state.clear()
    await message.answer(
        f"Добавлено: {len(added)}\n" + ", ".join(f"@{u}" for u in added),
        reply_markup=main_menu(),
    )


@router.message(Form.waiting_remove_target)
async def on_remove_target(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    m = USERNAME_RE.search(message.text or "")
    if not m:
        await message.answer("Укажите username.")
        return
    ok = await storage.remove_target(m.group(1))
    await state.clear()
    await message.answer(
        "Удалён." if ok else "Не найден.",
        reply_markup=main_menu(),
    )


# ---- texts ----
@router.callback_query(F.data == "txt:phrases_ru")
async def cb_txt_ru(call: CallbackQuery) -> None:
    from campaign import load_phrases

    texts = load_phrases("ru")
    body = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts[:80]))
    await send_long(call.message, f"Фразы RU ({len(texts)}):\n{body}")
    await call.answer()


@router.callback_query(F.data == "txt:phrases_en")
async def cb_txt_en(call: CallbackQuery) -> None:
    from campaign import load_phrases

    texts = load_phrases("en")
    body = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts[:80]))
    await send_long(call.message, f"Фразы EN ({len(texts)}):\n{body}")
    await call.answer()


@router.callback_query(F.data == "txt:up_ru")
async def cb_txt_up_ru(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.waiting_phrases_ru)
    await call.message.answer("Пришлите фразы RU — по одной на строку (или .txt файл).\n/cancel")
    await call.answer()


@router.callback_query(F.data == "txt:up_en")
async def cb_txt_up_en(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.waiting_phrases_en)
    await call.message.answer("Пришлите фразы EN — по одной на строку (или .txt файл).\n/cancel")
    await call.answer()


async def _save_phrases_from_message(message: Message, lang: str) -> int:
    from config import PHRASE_FILES
    from io import BytesIO

    if message.document:
        buffer = BytesIO()
        await message.bot.download(message.document, destination=buffer)
        text = buffer.getvalue().decode("utf-8", errors="ignore")
    else:
        text = message.text or ""

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0
    path = PHRASE_FILES[lang]
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(lines)


@router.message(Form.waiting_phrases_ru)
async def on_phrases_ru(message: Message, state: FSMContext) -> None:
    n = await _save_phrases_from_message(message, "ru")
    if n < 1:
        await message.answer("Пусто.")
        return
    await state.clear()
    await message.answer(f"Сохранено фраз RU: {n}", reply_markup=main_menu())


@router.message(Form.waiting_phrases_en)
async def on_phrases_en(message: Message, state: FSMContext) -> None:
    n = await _save_phrases_from_message(message, "en")
    if n < 1:
        await message.answer("Пусто.")
        return
    await state.clear()
    await message.answer(f"Сохранено фраз EN: {n}", reply_markup=main_menu())


@router.message(StateFilter(None))
async def fallback(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if message.document and (message.document.file_name or "").endswith(".session"):
        await message.answer("Чтобы добавить аккаунт: Меню → Аккаунты → Добавить .session")
        return
    await message.answer("Используйте кнопки меню.", reply_markup=main_menu())
