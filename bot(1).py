#!/usr/bin/env python3
"""
DISCIPLIN Bot v2 — Telegram бот с ИИ-анализом отчётов
Запуск: python bot.py
Зависимости: pip install -r requirements.txt
"""

import asyncio
import json
import os
import logging
import aiohttp
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ═══════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ — измени под себя
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN       = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY")

DATA_FILE       = Path("../data/reports.json")
USERS_FILE      = Path("../data/users.json")

MORNING_HOUR    = 8    # Час утреннего запроса плана (мск)
EVENING_HOUR    = 21   # Час вечернего запроса отчёта (мск)
REMINDER_MINS   = 30   # Интервал между напоминаниями (мин)
TIMEZONE        = "Europe/Moscow"

# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

bot       = Bot(token=BOT_TOKEN)
dp        = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# ───────────────────────────────────────────────────────────────
#  ХРАНИЛИЩЕ
# ───────────────────────────────────────────────────────────────
def load(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def this_month() -> str:
    return datetime.now().strftime("%Y-%m")


# ───────────────────────────────────────────────────────────────
#  ИИ-АНАЛИЗ (Claude API)
# ───────────────────────────────────────────────────────────────
async def ai_analyze_report(plan: dict, report: dict, user_name: str) -> dict:
    """
    Отправляет план + отчёт в Claude и получает структурированный анализ.
    Возвращает dict: { score, verdict, study_feedback, sport_feedback, flags, motivation }
    """
    prompt = f"""Ты строгий, но справедливый ИИ-наставник. Проанализируй отчёт пользователя.

ИМЯ: {user_name}

УТРЕННИЙ ПЛАН:
- Учёба: {plan.get('study', '—')}
- Спорт: {plan.get('sport', '—')}
- Саморазвитие: {plan.get('self_dev', '—')}

ВЕЧЕРНИЙ ОТЧЁТ:
- Учёба: {report.get('study', '—')}
- Спорт: {report.get('sport', '—')}
- Саморазвитие: {report.get('self_dev', '—')}
- Фотодоказательство: {'есть' if report.get('has_photo') else 'нет'}
- Пользователь сам подтвердил честность: {'да' if report.get('is_honest') else 'нет'}

ЗАДАЧА: Оцени выполнение плана и вырази мнение честно. Ответь СТРОГО в JSON формате без markdown:
{{
  "score": <число от 0 до 100>,
  "verdict": "<одно слово: ОТЛИЧНО/ХОРОШО/СОМНИТЕЛЬНО/ПРОВАЛ>",
  "study_feedback": "<1-2 предложения об учёбе>",
  "sport_feedback": "<1-2 предложения о спорте>",
  "consistency": "<оценка соответствия плана и отчёта: СОВПАДАЕТ/ЧАСТИЧНО/НЕ СОВПАДАЕТ>",
  "flags": ["<подозрительный момент 1>", "<подозрительный момент 2>"],
  "motivation": "<короткая мотивирующая или жёсткая фраза на ты, под настроение>",
  "needs_investigation": <true если есть серьёзные сомнения в честности, иначе false>
}}

Будь конкретным. Если отчёт расплывчатый — это подозрительно. Если нет фото при заявленной тренировке — отмечай."""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Claude API error: {resp.status}")
                    return _fallback_analysis()
                data = await resp.json()
                text = data["content"][0]["text"].strip()
                # Убираем возможные markdown-блоки
                text = text.replace("```json", "").replace("```", "").strip()
                return json.loads(text)
    except Exception as e:
        logger.error(f"AI analysis failed: {e}")
        return _fallback_analysis()

def _fallback_analysis() -> dict:
    return {
        "score": 50,
        "verdict": "НЕТ ДАННЫХ",
        "study_feedback": "Анализ временно недоступен.",
        "sport_feedback": "Анализ временно недоступен.",
        "consistency": "НЕ ОПРЕДЕЛЕНО",
        "flags": [],
        "motivation": "Продолжай работать.",
        "needs_investigation": False
    }

async def ai_monthly_summary(user_name: str, month_reports: list) -> str:
    """
    Итоговый месячный анализ — отправляет все отчёты, получает развёрнутый вывод.
    """
    reports_text = "\n\n".join([
        f"[{r['date']}] Учёба: {r.get('study','—')} | Спорт: {r.get('sport','—')} | "
        f"Фото: {'да' if r.get('has_photo') else 'нет'} | "
        f"ИИ-оценка: {r.get('ai_score', '?')}/100 | Честность: {'да' if r.get('is_honest') else 'нет'}"
        for r in month_reports
    ])

    prompt = f"""Проведи глубокий месячный анализ дисциплины пользователя {user_name}.

ДАННЫЕ ЗА МЕСЯЦ:
{reports_text}

Напиши развёрнутый отчёт (5-8 предложений) на русском:
1. Общая оценка дисциплины
2. Тренды (улучшение/ухудшение)
3. Слабые места (учёба vs спорт)
4. Подозрительные паттерны (если отчёты без фото, расплывчатые описания)
5. Конкретные рекомендации на следующий месяц

Будь честным и прямым. Не сюсюкай."""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                data = await resp.json()
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Monthly AI summary failed: {e}")
        return "Ежемесячный ИИ-анализ временно недоступен."


# ───────────────────────────────────────────────────────────────
#  ФОРМАТИРОВАНИЕ ИИ-ОТВЕТА
# ───────────────────────────────────────────────────────────────
def format_ai_result(analysis: dict) -> str:
    score      = analysis.get("score", "?")
    verdict    = analysis.get("verdict", "—")
    consistency = analysis.get("consistency", "—")
    flags      = analysis.get("flags", [])
    motivation = analysis.get("motivation", "")

    verdict_emoji = {
        "ОТЛИЧНО": "🟢", "ХОРОШО": "🟡",
        "СОМНИТЕЛЬНО": "🟠", "ПРОВАЛ": "🔴"
    }.get(verdict, "⚪")

    bar = "█" * (score // 10) + "░" * (10 - score // 10)

    text = (
        f"\n🤖 *ИИ-АНАЛИЗ ОТЧЁТА*\n"
        f"{'─'*28}\n"
        f"{verdict_emoji} Вердикт: *{verdict}*\n"
        f"📊 Оценка: `{bar}` {score}/100\n"
        f"🔗 Соответствие плану: {consistency}\n\n"
        f"📖 {analysis.get('study_feedback','')}\n"
        f"🏋️ {analysis.get('sport_feedback','')}\n"
    )

    if flags:
        text += f"\n⚠️ *Замечания:*\n"
        for flag in flags[:3]:
            text += f"  • {flag}\n"

    if analysis.get("needs_investigation"):
        text += "\n🚨 *Высокий риск неточности в отчёте — зафиксировано.*\n"

    text += f"\n💬 _{motivation}_"
    return text


# ───────────────────────────────────────────────────────────────
#  FSM СОСТОЯНИЯ
# ───────────────────────────────────────────────────────────────
class Plan(StatesGroup):
    study    = State()
    sport    = State()
    self_dev = State()

class Report(StatesGroup):
    study    = State()
    sport    = State()
    self_dev = State()
    photo    = State()
    confirm  = State()


# ───────────────────────────────────────────────────────────────
#  /start — РЕГИСТРАЦИЯ
# ───────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    users = load(USERS_FILE)
    uid   = str(message.from_user.id)

    if uid not in users:
        users[uid] = {
            "username":      message.from_user.username or "",
            "first_name":    message.from_user.first_name or "User",
            "registered_at": datetime.now().isoformat(),
            "streak":        0,
            "total_days":    0,
            "dishonesty_count": 0,
            "ai_avg_score":  0,
            "ai_reports_count": 0
        }
        save(USERS_FILE, users)
        name = message.from_user.first_name
        await message.answer(
            f"🔥 *Добро пожаловать, {name}!*\n\n"
            "Я — беспощадный бот-надзиратель с ИИ-детектором лжи.\n\n"
            "📋 *Как работаю:*\n"
            "• 08:00 — требую план (3 раздела)\n"
            "• 21:00 — требую отчёт + фото\n"
            "• Каждые 30 мин — напоминания если молчишь\n"
            "• После отчёта — ИИ анализирует твою честность\n"
            "• Раз в месяц — полный разбор полётов\n\n"
            "Пиши /plan чтобы начать прямо сейчас.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"Ты уже зарегистрирован!\n"
            "/plan — план | /report — отчёт | /status — статус"
        )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Команды:*\n\n"
        "/plan — утренний план\n"
        "/report — вечерний отчёт\n"
        "/status — статус сегодня\n"
        "/stats — статистика месяца\n"
        "/streak — серия дней\n"
        "/compare — сравнение с другом\n"
        "/analyze — ИИ-анализ последнего отчёта\n",
        parse_mode="Markdown"
    )


# ───────────────────────────────────────────────────────────────
#  ПЛАН НА ДЕНЬ
# ───────────────────────────────────────────────────────────────
@dp.message(Command("plan"))
async def cmd_plan(message: types.Message, state: FSMContext):
    uid      = str(message.from_user.id)
    reports  = load(DATA_FILE)

    if reports.get(uid, {}).get(today(), {}).get("plan"):
        await message.answer("✅ План на сегодня уже сдан. Используй /status.")
        return

    await message.answer(
        "📋 *ПЛАН НА ДЕНЬ* — раздел 1/3\n\n"
        "📖 *УЧЁБА*\n"
        "Что конкретно планируешь сделать?\n"
        "_(предмет, тема, количество часов/задач)_",
        parse_mode="Markdown"
    )
    await state.set_state(Plan.study)

@dp.message(Plan.study)
async def plan_study(message: types.Message, state: FSMContext):
    await state.update_data(study=message.text)
    await message.answer(
        "📋 *ПЛАН НА ДЕНЬ* — раздел 2/3\n\n"
        "🏋️ *СПОРТ*\n"
        "Что планируешь сделать?\n"
        "_(тип тренировки, длительность, упражнения)_",
        parse_mode="Markdown"
    )
    await state.set_state(Plan.sport)

@dp.message(Plan.sport)
async def plan_sport(message: types.Message, state: FSMContext):
    await state.update_data(sport=message.text)
    await message.answer(
        "📋 *ПЛАН НА ДЕНЬ* — раздел 3/3\n\n"
        "🧠 *САМОРАЗВИТИЕ*\n"
        "_(книги, навыки, проекты — или «нет»)_",
        parse_mode="Markdown"
    )
    await state.set_state(Plan.self_dev)

@dp.message(Plan.self_dev)
async def plan_self_dev(message: types.Message, state: FSMContext):
    d = await state.get_data()
    d["self_dev"] = message.text

    reports = load(DATA_FILE)
    uid     = str(message.from_user.id)
    t       = today()

    reports.setdefault(uid, {}).setdefault(t, {})
    reports[uid][t]["plan"] = {
        "study":    d["study"],
        "sport":    d["sport"],
        "self_dev": d["self_dev"],
        "submitted_at": datetime.now().isoformat()
    }
    save(DATA_FILE, reports)
    await state.clear()

    await message.answer(
        "✅ *ПЛАН ПРИНЯТ!*\n\n"
        f"📖 Учёба: {d['study']}\n"
        f"🏋️ Спорт: {d['sport']}\n"
        f"🧠 Саморазвитие: {d['self_dev']}\n\n"
        "Действуй. В 21:00 приду за отчётом. 😈",
        parse_mode="Markdown"
    )


# ───────────────────────────────────────────────────────────────
#  ВЕЧЕРНИЙ ОТЧЁТ
# ───────────────────────────────────────────────────────────────
@dp.message(Command("report"))
async def cmd_report(message: types.Message, state: FSMContext):
    uid     = str(message.from_user.id)
    reports = load(DATA_FILE)
    t       = today()

    if not reports.get(uid, {}).get(t, {}).get("plan"):
        await message.answer("⚠️ Сначала сдай план: /plan")
        return
    if reports.get(uid, {}).get(t, {}).get("report"):
        await message.answer("✅ Отчёт уже сдан. Используй /status.")
        return

    plan = reports[uid][t]["plan"]
    await message.answer(
        "🌙 *ВЕЧЕРНИЙ ОТЧЁТ* — раздел 1/3\n\n"
        f"_Твой план по учёбе: {plan['study']}_\n\n"
        "📖 *Что реально сделал по учёбе?*\n"
        "Пиши конкретно — ИИ сравнит с планом.",
        parse_mode="Markdown"
    )
    await state.set_state(Report.study)

@dp.message(Report.study)
async def report_study(message: types.Message, state: FSMContext):
    await state.update_data(study=message.text)
    await message.answer(
        "🌙 *ВЕЧЕРНИЙ ОТЧЁТ* — раздел 2/3\n\n"
        "🏋️ *Что сделал по спорту?*\n"
        "_(если ничего — честно напиши «не тренировался»)_",
        parse_mode="Markdown"
    )
    await state.set_state(Report.sport)

@dp.message(Report.sport)
async def report_sport(message: types.Message, state: FSMContext):
    await state.update_data(sport=message.text)
    await message.answer(
        "🌙 *ВЕЧЕРНИЙ ОТЧЁТ* — раздел 3/3\n\n"
        "🧠 *Саморазвитие — что сделал?*",
        parse_mode="Markdown"
    )
    await state.set_state(Report.self_dev)

@dp.message(Report.self_dev)
async def report_self_dev(message: types.Message, state: FSMContext):
    await state.update_data(self_dev=message.text)
    await message.answer(
        "📸 *ФОТОДОКАЗАТЕЛЬСТВО*\n\n"
        "Отправь фото/видео подтверждающее работу.\n"
        "_(скрин конспекта, фото с тренировки, и т.д.)_\n\n"
        "Нет фото? Напиши «нет» — это снизит оценку ИИ.",
        parse_mode="Markdown"
    )
    await state.set_state(Report.photo)

@dp.message(Report.photo, F.photo | F.video | F.document)
async def report_photo_media(message: types.Message, state: FSMContext):
    await state.update_data(has_photo=True)
    await _ask_honesty(message, state)

@dp.message(Report.photo, F.text)
async def report_photo_text(message: types.Message, state: FSMContext):
    await state.update_data(has_photo=False)
    await _ask_honesty(message, state)

async def _ask_honesty(message: types.Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="✅ Всё правда", callback_data="honest_yes"))
    kb.add(InlineKeyboardButton(text="😔 Есть неточности", callback_data="honest_no"))
    await message.answer(
        "⚖️ *ПОСЛЕДНИЙ ВОПРОС*\n\n"
        "Всё написанное — *полная правда*?\n\n"
        "ИИ всё равно проверит отчёт на честность.\n"
        "Признание сейчас — лучше, чем быть пойманным.",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )
    await state.set_state(Report.confirm)

@dp.callback_query(Report.confirm, F.data.in_({"honest_yes", "honest_no"}))
async def report_confirm(callback: types.CallbackQuery, state: FSMContext):
    d          = await state.get_data()
    is_honest  = callback.data == "honest_yes"
    uid        = str(callback.from_user.id)
    t          = today()

    reports = load(DATA_FILE)
    users   = load(USERS_FILE)

    report_data = {
        "study":       d.get("study", ""),
        "sport":       d.get("sport", ""),
        "self_dev":    d.get("self_dev", ""),
        "has_photo":   d.get("has_photo", False),
        "is_honest":   is_honest,
        "submitted_at": datetime.now().isoformat(),
        "ai_score":    None,
        "ai_verdict":  None,
        "ai_flags":    []
    }
    reports[uid][t]["report"] = report_data

    # Серия и статистика
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    had_yday  = reports.get(uid, {}).get(yesterday, {}).get("report")
    if uid in users:
        users[uid]["total_days"] = users[uid].get("total_days", 0) + 1
        users[uid]["streak"]     = users[uid].get("streak", 0) + 1 if had_yday else 1
        if not is_honest:
            users[uid]["dishonesty_count"] = users[uid].get("dishonesty_count", 0) + 1
        save(USERS_FILE, users)

    save(DATA_FILE, reports)
    await state.clear()
    await callback.message.edit_reply_markup()

    streak = users.get(uid, {}).get("streak", 1)
    if is_honest:
        await callback.message.answer(
            f"✅ *ОТЧЁТ ПРИНЯТ!* 🔥 Серия: {streak} дней\n\n"
            "Запускаю ИИ-анализ отчёта...",
            parse_mode="Markdown"
        )
    else:
        await callback.message.answer(
            "😤 Ценю честность. Зафиксировано.\n\n"
            "Запускаю ИИ-анализ отчёта...",
            parse_mode="Markdown"
        )

    # ── Запускаем ИИ-анализ ──
    plan = reports[uid][t].get("plan", {})
    user_name = users.get(uid, {}).get("first_name", "пользователь")

    analysis = await ai_analyze_report(plan, report_data, user_name)

    # Сохраняем результаты ИИ
    reports = load(DATA_FILE)
    reports[uid][t]["report"]["ai_score"]   = analysis.get("score")
    reports[uid][t]["report"]["ai_verdict"] = analysis.get("verdict")
    reports[uid][t]["report"]["ai_flags"]   = analysis.get("flags", [])
    reports[uid][t]["report"]["ai_needs_investigation"] = analysis.get("needs_investigation", False)
    save(DATA_FILE, reports)

    # Обновляем средний ИИ-балл пользователя
    users = load(USERS_FILE)
    if uid in users and analysis.get("score") is not None:
        cnt = users[uid].get("ai_reports_count", 0)
        avg = users[uid].get("ai_avg_score", 0)
        users[uid]["ai_avg_score"]     = round((avg * cnt + analysis["score"]) / (cnt + 1), 1)
        users[uid]["ai_reports_count"] = cnt + 1
        save(USERS_FILE, users)

    ai_text = format_ai_result(analysis)
    await callback.message.answer(ai_text, parse_mode="Markdown")

    # Если ИИ подозревает обман — уведомить обоих
    if analysis.get("needs_investigation"):
        await _notify_partner_about_suspicion(uid, user_name, analysis)

    await callback.answer()


async def _notify_partner_about_suspicion(uid: str, user_name: str, analysis: dict):
    """Уведомить друга если ИИ нашёл подозрительный отчёт"""
    users = load(USERS_FILE)
    flags_text = "\n".join(f"• {f}" for f in analysis.get("flags", []))
    for partner_uid, partner in users.items():
        if partner_uid != uid:
            try:
                await bot.send_message(
                    int(partner_uid),
                    f"🚨 *ИИ АЛЕРТ*\n\n"
                    f"В отчёте {user_name} обнаружены подозрительные моменты:\n\n"
                    f"{flags_text}\n\n"
                    f"ИИ-оценка: {analysis.get('score')}/100\n"
                    f"Вердикт: {analysis.get('verdict')}\n\n"
                    f"_Спроси его об этом лично._",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify partner {partner_uid}: {e}")


# ───────────────────────────────────────────────────────────────
#  КОМАНДА: /analyze — ИИ-анализ последнего отчёта вручную
# ───────────────────────────────────────────────────────────────
@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    uid     = str(message.from_user.id)
    reports = load(DATA_FILE)
    t       = today()

    if not reports.get(uid, {}).get(t, {}).get("report"):
        await message.answer("Нет отчёта за сегодня. Сначала /report")
        return

    plan   = reports[uid][t].get("plan", {})
    report = reports[uid][t]["report"]
    users  = load(USERS_FILE)
    name   = users.get(uid, {}).get("first_name", "пользователь")

    await message.answer("🤖 Запускаю ИИ-анализ...")
    analysis = await ai_analyze_report(plan, report, name)
    await message.answer(format_ai_result(analysis), parse_mode="Markdown")


# ───────────────────────────────────────────────────────────────
#  СТАТУС / СТАТИСТИКА / СЕРИЯ / СРАВНЕНИЕ
# ───────────────────────────────────────────────────────────────
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    uid     = str(message.from_user.id)
    reports = load(DATA_FILE)
    t       = today()
    day     = reports.get(uid, {}).get(t, {})

    if not day:
        await message.answer("Сегодня нет данных. Начни с /plan")
        return

    plan   = day.get("plan")
    report = day.get("report")
    text   = f"📊 *Статус на {t}*\n\n"

    if plan:
        text += f"✅ *ПЛАН сдан*\n📖 {plan['study']}\n🏋️ {plan['sport']}\n🧠 {plan['self_dev']}\n\n"
    else:
        text += "❌ *ПЛАН не сдан* → /plan\n\n"

    if report:
        ai_score   = report.get("ai_score")
        ai_verdict = report.get("ai_verdict", "")
        score_str  = f" | ИИ: {ai_score}/100 ({ai_verdict})" if ai_score is not None else ""
        text += (
            f"✅ *ОТЧЁТ сдан*{score_str}\n"
            f"📸 Фото: {'есть' if report['has_photo'] else 'нет'}\n"
            f"⚖️ Честность: {'подтверждена' if report['is_honest'] else 'под вопросом'}\n"
        )
        if report.get("ai_flags"):
            text += "⚠️ Замечания ИИ: " + ", ".join(report["ai_flags"][:2])
    else:
        text += "⏳ *ОТЧЁТ не сдан* → /report"

    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    uid     = str(message.from_user.id)
    reports = load(DATA_FILE)
    users   = load(USERS_FILE)
    month   = this_month()

    month_data = {d: v for d, v in reports.get(uid, {}).items() if d.startswith(month)}
    total        = len(month_data)
    with_plan    = sum(1 for v in month_data.values() if v.get("plan"))
    with_report  = sum(1 for v in month_data.values() if v.get("report"))
    with_photo   = sum(1 for v in month_data.values() if v.get("report", {}).get("has_photo"))
    dishonest    = sum(1 for v in month_data.values() if v.get("report") and not v["report"].get("is_honest"))
    ai_scores    = [v["report"]["ai_score"] for v in month_data.values()
                    if v.get("report", {}).get("ai_score") is not None]
    avg_ai       = round(sum(ai_scores) / len(ai_scores)) if ai_scores else "—"
    streak       = users.get(uid, {}).get("streak", 0)

    await message.answer(
        f"📈 *Статистика за {month}*\n\n"
        f"📅 Дней с данными: {total}\n"
        f"📋 Планов: {with_plan}\n"
        f"📝 Отчётов: {with_report}\n"
        f"📸 С фото: {with_photo}\n"
        f"😔 Признаний в неточности: {dishonest}\n"
        f"🤖 Средний ИИ-балл: {avg_ai}/100\n\n"
        f"🔥 Серия: {streak} дней\n"
        f"📊 Дисциплина: {int(with_report / max(total, 1) * 100)}%",
        parse_mode="Markdown"
    )


@dp.message(Command("streak"))
async def cmd_streak(message: types.Message):
    uid    = str(message.from_user.id)
    users  = load(USERS_FILE)
    user   = users.get(uid, {})
    streak = user.get("streak", 0)
    emoji  = "🔥" if streak >= 7 else "⚡" if streak >= 3 else "💤"
    await message.answer(
        f"{emoji} *Серия: {streak} дней*\n"
        f"Всего отчётных дней: {user.get('total_days', 0)}\n"
        f"🤖 Средний ИИ-балл: {user.get('ai_avg_score', '—')}/100",
        parse_mode="Markdown"
    )


@dp.message(Command("compare"))
async def cmd_compare(message: types.Message):
    reports = load(DATA_FILE)
    users   = load(USERS_FILE)
    t       = today()
    text    = f"👥 *Сравнение — {t}*\n\n"

    for uid, user in users.items():
        name      = user.get("first_name", "?")
        day       = reports.get(uid, {}).get(t, {})
        plan_ok   = "✅" if day.get("plan") else "❌"
        report    = day.get("report")
        rep_ok    = "✅" if report else "⏳"
        ai_score  = report.get("ai_score") if report else None
        ai_str    = f" | ИИ: {ai_score}/100" if ai_score is not None else ""
        streak    = user.get("streak", 0)
        avg_score = user.get("ai_avg_score", "—")

        text += (
            f"👤 *{name}*\n"
            f"  план {plan_ok} | отчёт {rep_ok}{ai_str}\n"
            f"  🔥 серия: {streak} | ср. ИИ-балл: {avg_score}/100\n\n"
        )

    await message.answer(text, parse_mode="Markdown")


# ───────────────────────────────────────────────────────────────
#  ПЛАНИРОВЩИК — УВЕДОМЛЕНИЯ
# ───────────────────────────────────────────────────────────────
NAG_MORNING = [
    "☀️ *Доброе утро!* Новый день. Сдай план: /plan",
    "📢 Ты ещё не сдал план. Это не опционально. /plan",
    "⏰ Уже {time}. Плана нет. /plan — прямо сейчас.",
    "🚨 ПЛАН! Без плана нет цели. Без цели нет результата. /plan",
    "😤 Последний раз вежливо прошу. /plan",
    "💀 Это уже неприлично. /plan"
]

NAG_EVENING = [
    "🌙 Рабочий день закончен. Отчёт: /report",
    "📝 Отчёт ещё не сдан. /report",
    "⏰ {time}. Без отчёта — день не засчитан. /report",
    "😡 Серьёзно? Ещё нет отчёта? /report",
    "🚨 До 23:00 осталось мало. Серия под угрозой. /report",
    "💀 Последний шанс. /report"
]

async def _blast(users, reports, t, hour_limit_max: int, key: str, nag_list: list, count_field: str):
    now = datetime.now()
    if now.hour >= hour_limit_max:
        return
    for uid, user in users.items():
        has_item = reports.get(uid, {}).get(t, {}).get(key)
        if not has_item:
            cnt = reports.get(uid, {}).get(t, {}).get(count_field, 0)
            msg = nag_list[min(cnt, len(nag_list)-1)].replace("{time}", now.strftime("%H:%M"))
            try:
                await bot.send_message(int(uid), msg, parse_mode="Markdown")
                reports.setdefault(uid, {}).setdefault(t, {})[count_field] = cnt + 1
            except Exception as e:
                logger.error(f"Blast error {uid}: {e}")
    save(DATA_FILE, reports)

async def job_morning():
    users   = load(USERS_FILE)
    reports = load(DATA_FILE)
    t       = today()
    for uid, user in users.items():
        if not reports.get(uid, {}).get(t, {}).get("plan"):
            try:
                await bot.send_message(
                    int(uid),
                    "☀️ *Доброе утро!*\n\nНовый день. Сдай план: /plan",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Morning job error {uid}: {e}")

async def job_plan_reminder():
    users   = load(USERS_FILE)
    reports = load(DATA_FILE)
    await _blast(users, reports, today(), 10, "plan", NAG_MORNING, "morning_nag_count")

async def job_evening():
    users   = load(USERS_FILE)
    reports = load(DATA_FILE)
    t       = today()
    for uid, user in users.items():
        if not reports.get(uid, {}).get(t, {}).get("report"):
            try:
                await bot.send_message(
                    int(uid),
                    "🌙 *Рабочий день закончен!*\n\nВремя отчитываться: /report",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Evening job error {uid}: {e}")

async def job_report_reminder():
    users   = load(USERS_FILE)
    reports = load(DATA_FILE)
    await _blast(users, reports, today(), 23, "report", NAG_EVENING, "evening_nag_count")

async def job_midnight():
    """23:05 — обнуляем серию пропустившим, шеймим"""
    users   = load(USERS_FILE)
    reports = load(DATA_FILE)
    t       = today()
    changed = False
    for uid, user in users.items():
        has_plan   = reports.get(uid, {}).get(t, {}).get("plan")
        has_report = reports.get(uid, {}).get(t, {}).get("report")
        if not has_report:
            old_streak       = user.get("streak", 0)
            users[uid]["streak"] = 0
            changed = True
            msg = (
                "💀 *День провален.*\nОтчёт не сдан. Серия обнулена."
                if has_plan else
                "💀 *Полный провал.* Ни плана, ни отчёта. Серия обнулена."
            )
            if old_streak > 0:
                msg += f"\n\n_Была серия {old_streak} дней. Теперь 0._"
            try:
                await bot.send_message(int(uid), msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Midnight job error {uid}: {e}")
    if changed:
        save(USERS_FILE, users)

async def job_monthly():
    """Последний день месяца — ИИ-разбор полётов за весяц"""
    users   = load(USERS_FILE)
    reports = load(DATA_FILE)
    month   = this_month()

    for uid, user in users.items():
        month_days = {d: v for d, v in reports.get(uid, {}).items() if d.startswith(month)}
        if not month_days:
            continue

        report_list = []
        for d, v in sorted(month_days.items()):
            r = v.get("report")
            if r:
                report_list.append({
                    "date":       d,
                    "study":      r.get("study", ""),
                    "sport":      r.get("sport", ""),
                    "has_photo":  r.get("has_photo", False),
                    "is_honest":  r.get("is_honest", True),
                    "ai_score":   r.get("ai_score")
                })

        name    = user.get("first_name", "пользователь")
        summary = await ai_monthly_summary(name, report_list)

        try:
            await bot.send_message(
                int(uid),
                f"📊 *ЕЖЕМЕСЯЧНЫЙ ИИ-АНАЛИЗ — {month}*\n\n{summary}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Monthly job error {uid}: {e}")


# ───────────────────────────────────────────────────────────────
#  ЗАПУСК
# ───────────────────────────────────────────────────────────────
async def main():
    scheduler.add_job(job_morning,         "cron", hour=MORNING_HOUR, minute=0)
    scheduler.add_job(job_plan_reminder,   "interval", minutes=REMINDER_MINS)
    scheduler.add_job(job_evening,         "cron", hour=EVENING_HOUR, minute=0)
    scheduler.add_job(job_report_reminder, "interval", minutes=REMINDER_MINS)
    scheduler.add_job(job_midnight,        "cron", hour=23, minute=5)
    scheduler.add_job(job_monthly,         "cron", day="last", hour=23, minute=0)
    scheduler.start()

    logger.info("🤖 DISCIPLIN Bot v2 запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
