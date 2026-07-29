import asyncio
import calendar
import html
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from services import (
    ABOUT_TEXT,
    CONSULTATION_TEXT,
    CONTACTS_TEXT,
    EMERGENCY_TEXT,
    PRICE_TEXT,
    START_TEXT,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dp = Dispatcher()

APP_VERSION = "schedule-admin-v2"

# Локальная база данных создастся рядом с bot.py автоматически.
DB_PATH = Path(__file__).resolve().parent / "bookings.db"

# Начальное расписание создаётся только при первом запуске новой базы.
# После этого специалист меняет его прямо в Telegram командой /setday.
DEFAULT_TIME_SLOTS = ("10:00", "12:00", "15:00", "17:00", "19:00")
DEFAULT_WORKING_WEEKDAYS = {0, 1, 2, 3, 4}

# На сколько месяцев вперёд разрешена запись.
MAX_MONTHS_AHEAD = 6

MONTHS_RU = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)

WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

WEEKDAYS_RU_FULL = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

WEEKDAY_ALIASES = {
    "1": 0,
    "пн": 0,
    "понедельник": 0,
    "2": 1,
    "вт": 1,
    "вторник": 1,
    "3": 2,
    "ср": 2,
    "среда": 2,
    "4": 3,
    "чт": 3,
    "четверг": 3,
    "5": 4,
    "пт": 4,
    "пятница": 4,
    "6": 5,
    "сб": 5,
    "суббота": 5,
    "7": 6,
    "вс": 6,
    "воскресенье": 6,
}

DAY_OFF_WORDS = {
    "выходной",
    "выходные",
    "нерабочий",
    "нерабочий день",
    "off",
    "нет",
}


class Appointment(StatesGroup):
    name = State()
    appointment_date = State()
    appointment_time = State()
    contact = State()
    topic = State()
    confirm = State()


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Записаться")],
        [
            KeyboardButton(text="👤 О психологе"),
            KeyboardButton(text="💬 Как проходит консультация"),
        ],
        [
            KeyboardButton(text="💳 Стоимость"),
            KeyboardButton(text="📞 Контакты"),
        ],
        [KeyboardButton(text="🚨 Важная информация")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите раздел",
)

cancel_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
)

contact_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📱 Отправить мой номер",
                request_contact=True,
            )
        ],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

topic_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

confirm_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Подтвердить")],
        [KeyboardButton(text="📅 Изменить дату и время")],
        [KeyboardButton(text="✏️ Заполнить заново")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)



def init_db() -> None:
    """Создаёт таблицу заявок при первом запуске."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                client_name TEXT NOT NULL,
                contact TEXT NOT NULL,
                topic TEXT NOT NULL,
                telegram_user_id INTEGER,
                telegram_username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (appointment_date, appointment_time)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS closures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_schedule (
                weekday INTEGER PRIMARY KEY
                    CHECK (weekday BETWEEN 0 AND 6),
                time_slots TEXT NOT NULL DEFAULT ''
            )
            """
        )

        schedule_count = connection.execute(
            "SELECT COUNT(*) FROM weekly_schedule"
        ).fetchone()[0]

        if schedule_count == 0:
            for weekday in range(7):
                slots = (
                    ",".join(DEFAULT_TIME_SLOTS)
                    if weekday in DEFAULT_WORKING_WEEKDAYS
                    else ""
                )
                connection.execute(
                    """
                    INSERT INTO weekly_schedule (
                        weekday,
                        time_slots
                    )
                    VALUES (?, ?)
                    """,
                    (weekday, slots),
                )

        connection.commit()



def parse_weekday(value: str) -> int | None:
    """Преобразует название или номер дня недели в 0–6."""
    return WEEKDAY_ALIASES.get(value.strip().lower())


def parse_time(value: str) -> str:
    """Проверяет время в формате ЧЧ:ММ и возвращает его."""
    normalized = value.strip()

    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized):
        raise ValueError("Время должно быть в формате ЧЧ:ММ.")

    return normalized


def sort_time_slots(slots: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Удаляет повторы и сортирует время по возрастанию."""
    unique_slots = set(slots)

    return tuple(
        sorted(
            unique_slots,
            key=lambda item: (
                int(item.split(":")[0]),
                int(item.split(":")[1]),
            ),
        )
    )


def build_slots_from_range(
    range_value: str,
    step_minutes: int = 60,
) -> tuple[str, ...]:
    """
    Создаёт время начала консультаций из диапазона.

    Пример: 10:00-18:00 с шагом 60 минут
    создаст 10:00, 11:00, ..., 17:00.
    """
    if not 15 <= step_minutes <= 240:
        raise ValueError(
            "Шаг должен быть от 15 до 240 минут."
        )

    parts = range_value.split("-", maxsplit=1)

    if len(parts) != 2:
        raise ValueError(
            "Диапазон должен выглядеть так: 10:00-18:00."
        )

    start_value = parse_time(parts[0])
    end_value = parse_time(parts[1])

    start_time = datetime.strptime(start_value, "%H:%M")
    end_time = datetime.strptime(end_value, "%H:%M")

    if end_time <= start_time:
        raise ValueError(
            "Конец рабочего дня должен быть позже начала."
        )

    slots: list[str] = []
    current_time = start_time

    while current_time < end_time:
        slots.append(current_time.strftime("%H:%M"))
        current_time += timedelta(minutes=step_minutes)

    return tuple(slots)


def parse_schedule_slots(parts: list[str]) -> tuple[str, ...]:
    """Разбирает выходной, диапазон или список точного времени."""
    if not parts:
        raise ValueError("Не указано рабочее время.")

    joined = " ".join(parts).strip().lower()

    if joined in DAY_OFF_WORDS:
        return ()

    first_value = parts[0].strip()

    if "-" in first_value:
        if len(parts) > 2:
            raise ValueError(
                "После диапазона можно указать только шаг в минутах."
            )

        step_minutes = 60

        if len(parts) == 2:
            if not parts[1].isdigit():
                raise ValueError(
                    "Шаг должен быть числом минут, например 30 или 60."
                )
            step_minutes = int(parts[1])

        return build_slots_from_range(
            first_value,
            step_minutes,
        )

    normalized_parts: list[str] = []

    for part in parts:
        for value in part.replace(",", " ").split():
            normalized_parts.append(parse_time(value))

    if not normalized_parts:
        raise ValueError("Не указано рабочее время.")

    return sort_time_slots(normalized_parts)


def get_weekday_slots(weekday: int) -> tuple[str, ...]:
    """Возвращает время начала консультаций для дня недели."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        row = connection.execute(
            """
            SELECT time_slots
            FROM weekly_schedule
            WHERE weekday = ?
            """,
            (weekday,),
        ).fetchone()

    if not row or not str(row[0]).strip():
        return ()

    return sort_time_slots(
        [
            value.strip()
            for value in str(row[0]).split(",")
            if value.strip()
        ]
    )


def get_weekly_schedule() -> dict[int, tuple[str, ...]]:
    """Возвращает полное еженедельное расписание."""
    return {
        weekday: get_weekday_slots(weekday)
        for weekday in range(7)
    }


def set_weekday_slots(
    weekday: int,
    slots: tuple[str, ...],
) -> None:
    """Сохраняет рабочее время выбранного дня недели."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        connection.execute(
            """
            INSERT INTO weekly_schedule (
                weekday,
                time_slots
            )
            VALUES (?, ?)
            ON CONFLICT(weekday)
            DO UPDATE SET time_slots = excluded.time_slots
            """,
            (weekday, ",".join(slots)),
        )
        connection.commit()


def get_schedule_conflicts(
    weekday: int,
    new_slots: tuple[str, ...],
) -> list[tuple[str, str, str]]:
    """
    Возвращает будущие записи, которые исчезнут
    после изменения расписания.
    """
    allowed_slots = set(new_slots)

    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        rows = connection.execute(
            """
            SELECT
                appointment_date,
                appointment_time,
                client_name
            FROM bookings
            WHERE appointment_date >= ?
            ORDER BY appointment_date, appointment_time
            """,
            (date.today().isoformat(),),
        ).fetchall()

    conflicts: list[tuple[str, str, str]] = []

    for appointment_date, appointment_time, client_name in rows:
        selected_date = date.fromisoformat(
            str(appointment_date)
        )

        if (
            selected_date.weekday() == weekday
            and str(appointment_time) not in allowed_slots
        ):
            conflicts.append(
                (
                    str(appointment_date),
                    str(appointment_time),
                    str(client_name),
                )
            )

    return conflicts


def parse_admin_date(value: str) -> date:
    """Преобразует дату администратора из ДД.ММ.ГГГГ."""
    return datetime.strptime(value, "%d.%m.%Y").date()


def format_date_value(value: date) -> str:
    """Форматирует объект date для сообщений."""
    return value.strftime("%d.%m.%Y")


def get_closure_reason(iso_date: str) -> str | None:
    """Возвращает причину, по которой специалист не работает."""
    selected_date = date.fromisoformat(iso_date)

    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        row = connection.execute(
            """
            SELECT reason
            FROM closures
            WHERE start_date <= ? AND end_date >= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (iso_date, iso_date),
        ).fetchone()

    if row:
        return str(row[0])

    if not get_weekday_slots(selected_date.weekday()):
        return "Выходной по еженедельному расписанию"

    return None


def add_closure(
    start_date: date,
    end_date: date,
    reason: str,
) -> int:
    """Добавляет отпуск, больничный или другой нерабочий период."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        cursor = connection.execute(
            """
            INSERT INTO closures (start_date, end_date, reason)
            VALUES (?, ?, ?)
            """,
            (
                start_date.isoformat(),
                end_date.isoformat(),
                reason,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def delete_closure(closure_id: int) -> bool:
    """Удаляет нерабочий период по номеру."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        cursor = connection.execute(
            "DELETE FROM closures WHERE id = ?",
            (closure_id,),
        )
        connection.commit()
        return cursor.rowcount > 0


def get_future_closures() -> list[tuple[int, str, str, str]]:
    """Возвращает текущие и будущие нерабочие периоды."""
    today_iso = date.today().isoformat()

    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        rows = connection.execute(
            """
            SELECT id, start_date, end_date, reason
            FROM closures
            WHERE end_date >= ?
            ORDER BY start_date, id
            """,
            (today_iso,),
        ).fetchall()

    return [
        (int(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in rows
    ]


def get_bookings_in_period(
    start_date: date,
    end_date: date,
) -> list[tuple[str, str, str]]:
    """Находит существующие записи внутри закрываемого периода."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        rows = connection.execute(
            """
            SELECT appointment_date, appointment_time, client_name
            FROM bookings
            WHERE appointment_date BETWEEN ? AND ?
            ORDER BY appointment_date, appointment_time
            """,
            (
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        ).fetchall()

    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in rows
    ]

def get_booked_slots(iso_date: str) -> set[str]:
    """Возвращает занятое время для выбранной даты."""
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        rows = connection.execute(
            """
            SELECT appointment_time
            FROM bookings
            WHERE appointment_date = ?
            """,
            (iso_date,),
        ).fetchall()

    return {str(row[0]) for row in rows}


def get_available_slots(iso_date: str) -> tuple[str, ...]:
    """Возвращает свободные интервалы только в рабочий день."""
    if get_closure_reason(iso_date) is not None:
        return ()

    selected_date = date.fromisoformat(iso_date)
    scheduled_slots = get_weekday_slots(
        selected_date.weekday()
    )
    booked_slots = get_booked_slots(iso_date)

    return tuple(
        time_slot
        for time_slot in scheduled_slots
        if time_slot not in booked_slots
    )


def is_slot_available(
    iso_date: str,
    appointment_time: str,
) -> bool:
    """Проверяет, свободны ли выбранные дата и время."""
    return appointment_time in get_available_slots(iso_date)


def save_booking(
    data: dict,
    telegram_user_id: int | None,
    telegram_username: str,
) -> bool:
    """
    Сохраняет подтверждённую запись.

    UNIQUE в таблице не позволяет двум пользователям
    занять одинаковые дату и время.
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as connection:
            connection.execute(
                """
                INSERT INTO bookings (
                    appointment_date,
                    appointment_time,
                    client_name,
                    contact,
                    topic,
                    telegram_user_id,
                    telegram_username
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data["appointment_date"]),
                    str(data["appointment_time"]),
                    str(data["name"]),
                    str(data["contact"]),
                    str(data.get("topic", "Не указана")),
                    telegram_user_id,
                    telegram_username,
                ),
            )
            connection.commit()
    except sqlite3.IntegrityError:
        return False

    return True


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Сдвигает месяц вперёд или назад."""
    month_number = year * 12 + month - 1 + delta
    return month_number // 12, month_number % 12 + 1


def month_index(year: int, month: int) -> int:
    """Преобразует год и месяц в число для сравнения."""
    return year * 12 + month


def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    """Создаёт встроенный календарь Telegram."""
    today = date.today()
    max_year, max_month = shift_month(
        today.year,
        today.month,
        MAX_MONTHS_AHEAD,
    )

    previous_year, previous_month = shift_month(year, month, -1)
    next_year, next_month = shift_month(year, month, 1)

    current_index = month_index(today.year, today.month)
    previous_index = month_index(previous_year, previous_month)
    next_index = month_index(next_year, next_month)
    max_index = month_index(max_year, max_month)

    previous_callback = (
        f"cal:nav:{previous_year}:{previous_month}"
        if previous_index >= current_index
        else "cal:noop"
    )
    next_callback = (
        f"cal:nav:{next_year}:{next_month}"
        if next_index <= max_index
        else "cal:noop"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="◀️",
                callback_data=previous_callback,
            ),
            InlineKeyboardButton(
                text=f"{MONTHS_RU[month]} {year}",
                callback_data="cal:noop",
            ),
            InlineKeyboardButton(
                text="▶️",
                callback_data=next_callback,
            ),
        ],
        [
            InlineKeyboardButton(
                text=weekday,
                callback_data="cal:noop",
            )
            for weekday in WEEKDAYS_RU
        ],
    ]

    month_calendar = calendar.Calendar(
        firstweekday=0
    ).monthdayscalendar(year, month)

    for week in month_calendar:
        week_row: list[InlineKeyboardButton] = []

        for day_number in week:
            if day_number == 0:
                week_row.append(
                    InlineKeyboardButton(
                        text=" ",
                        callback_data="cal:noop",
                    )
                )
                continue

            selected_date = date(year, month, day_number)

            iso_date = selected_date.isoformat()

            if selected_date < today:
                text = "·"
                callback_data = "cal:noop"
            else:
                closure_reason = get_closure_reason(iso_date)

                if closure_reason is not None:
                    text = f"{day_number}×"
                    callback_data = f"cal:closed:{iso_date}"
                elif not get_available_slots(iso_date):
                    text = f"{day_number}•"
                    callback_data = f"cal:full:{iso_date}"
                else:
                    text = str(day_number)
                    callback_data = f"cal:day:{iso_date}"

            week_row.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=callback_data,
                )
            )

        rows.append(week_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_time_keyboard(
    iso_date: str,
) -> InlineKeyboardMarkup | None:
    """Создаёт кнопки только со свободным временем."""
    available_slots = get_available_slots(iso_date)

    if not available_slots:
        return None

    rows: list[list[InlineKeyboardButton]] = []

    for index in range(0, len(available_slots), 2):
        row = [
            InlineKeyboardButton(
                text=time_slot,
                callback_data=f"time:{time_slot}",
            )
            for time_slot in available_slots[index:index + 2]
        ]
        rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_date(iso_date: str) -> str:
    """Преобразует 2026-07-29 в 29.07.2026."""
    selected_date = date.fromisoformat(iso_date)
    return selected_date.strftime("%d.%m.%Y")


def make_summary(data: dict) -> str:
    """Формирует итог заявки."""
    name = html.escape(str(data.get("name", "—")))

    appointment_date = (
        format_date(str(data["appointment_date"]))
        if data.get("appointment_date")
        else "—"
    )

    appointment_time = html.escape(
        str(data.get("appointment_time", "—"))
    )
    contact = html.escape(str(data.get("contact", "—")))
    topic = html.escape(
        str(data.get("topic", "Не указана"))
    )

    return (
        "<b>Проверьте заявку</b>\n\n"
        f"<b>Имя:</b> {name}\n"
        f"<b>Дата:</b> {appointment_date}\n"
        f"<b>Время:</b> {appointment_time}\n"
        f"<b>Контакт:</b> {contact}\n"
        f"<b>Тема обращения:</b> {topic}"
    )


async def ask_for_date(message: Message) -> None:
    """Показывает календарь начиная с текущего месяца."""
    today = date.today()

    await message.answer(
        "Выберите точную дату консультации:\n\n"
        "× — специалист не работает\n"
        "• — всё время уже занято",
        reply_markup=build_calendar(
            today.year,
            today.month,
        ),
    )


@dp.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        START_TEXT,
        reply_markup=main_menu,
    )


@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cancel_handler(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()

    await message.answer(
        "Заполнение заявки отменено.\n\n"
        "Выберите нужный раздел.",
        reply_markup=main_menu,
    )


@dp.message(F.text == "👤 О психологе")
async def about_handler(message: Message) -> None:
    await message.answer(
        ABOUT_TEXT,
        reply_markup=main_menu,
    )


@dp.message(F.text == "💬 Как проходит консультация")
async def consultation_handler(message: Message) -> None:
    await message.answer(
        CONSULTATION_TEXT,
        reply_markup=main_menu,
    )


@dp.message(F.text == "💳 Стоимость")
async def price_handler(message: Message) -> None:
    await message.answer(
        PRICE_TEXT,
        reply_markup=main_menu,
    )


@dp.message(F.text == "📞 Контакты")
async def contacts_handler(message: Message) -> None:
    await message.answer(
        CONTACTS_TEXT,
        reply_markup=main_menu,
    )


@dp.message(F.text == "🚨 Важная информация")
async def emergency_handler(message: Message) -> None:
    await message.answer(
        EMERGENCY_TEXT,
        reply_markup=main_menu,
    )


def message_from_admin(message: Message) -> bool:
    """Проверяет, что команду отправил владелец бота."""
    return (
        ADMIN_ID is not None
        and message.from_user is not None
        and message.from_user.id == ADMIN_ID
    )


@dp.message(Command("version"))
async def version_handler(message: Message) -> None:
    await message.answer(
        f"Версия бота: <code>{APP_VERSION}</code>"
    )


@dp.message(Command("schedule"))
async def schedule_handler(message: Message) -> None:
    """Показывает еженедельное расписание специалиста."""
    if not message_from_admin(message):
        await message.answer(
            "Эта команда доступна только администратору."
        )
        return

    schedule = get_weekly_schedule()
    lines = ["<b>Еженедельное расписание</b>\n"]

    for weekday in range(7):
        slots = schedule[weekday]
        value = ", ".join(slots) if slots else "выходной"

        lines.append(
            f"<b>{WEEKDAYS_RU_FULL[weekday]}:</b> {value}"
        )

    lines.extend(
        [
            "",
            "<b>Указать точное время:</b>",
            "<code>/setday пн 10:00 12:00 15:00</code>",
            "",
            "<b>Указать рабочий диапазон:</b>",
            "<code>/setday вт 10:00-18:00</code>",
            "По умолчанию шаг — 60 минут.",
            "",
            "<b>Диапазон с другим шагом:</b>",
            "<code>/setday ср 09:00-17:00 30</code>",
            "",
            "<b>Сделать день выходным:</b>",
            "<code>/setday вс выходной</code>",
        ]
    )

    await message.answer("\n".join(lines))


@dp.message(Command("setday"))
async def setday_handler(message: Message) -> None:
    """Изменяет рабочее время одного дня недели."""
    if not message_from_admin(message):
        await message.answer(
            "Эта команда доступна только администратору."
        )
        return

    parts = (message.text or "").split()

    if len(parts) < 3:
        await message.answer(
            "Используйте один из вариантов:\n\n"
            "<code>/setday пн 10:00 12:00 15:00</code>\n"
            "<code>/setday пн 10:00-18:00</code>\n"
            "<code>/setday пн 10:00-18:00 30</code>\n"
            "<code>/setday пн выходной</code>\n\n"
            "Текущее расписание: /schedule"
        )
        return

    weekday = parse_weekday(parts[1])

    if weekday is None:
        await message.answer(
            "Не удалось распознать день недели.\n\n"
            "Используйте: пн, вт, ср, чт, пт, сб или вс."
        )
        return

    try:
        new_slots = parse_schedule_slots(parts[2:])
    except ValueError as error:
        await message.answer(
            f"Не удалось изменить расписание: "
            f"{html.escape(str(error))}\n\n"
            "Примеры:\n"
            "<code>/setday пн 10:00 12:00 15:00</code>\n"
            "<code>/setday пн 10:00-18:00</code>\n"
            "<code>/setday пн выходной</code>"
        )
        return

    conflicts = get_schedule_conflicts(
        weekday,
        new_slots,
    )

    if conflicts:
        lines: list[str] = []

        for (
            appointment_date,
            appointment_time,
            client_name,
        ) in conflicts[:10]:
            lines.append(
                f"• {format_date(appointment_date)} в "
                f"{html.escape(appointment_time)} — "
                f"{html.escape(client_name)}"
            )

        extra_count = len(conflicts) - len(lines)
        extra_text = (
            f"\nЕщё записей: {extra_count}."
            if extra_count > 0
            else ""
        )

        await message.answer(
            "Расписание не изменено: некоторые будущие "
            "записи перестанут входить в новое время.\n\n"
            + "\n".join(lines)
            + extra_text
            + "\n\nСначала перенесите эти записи."
        )
        return

    set_weekday_slots(
        weekday=weekday,
        slots=new_slots,
    )

    if new_slots:
        result = ", ".join(new_slots)
        status_text = (
            f"✅ {WEEKDAYS_RU_FULL[weekday]} теперь рабочий день.\n"
            f"<b>Время начала консультаций:</b> {result}"
        )
    else:
        status_text = (
            f"✅ {WEEKDAYS_RU_FULL[weekday]} теперь выходной."
        )

    await message.answer(
        status_text
        + "\n\nПолное расписание: /schedule"
    )


@dp.message(Command("close"))
async def close_period_handler(message: Message) -> None:
    """
    Закрывает период для записи.

    Формат:
    /close 10.08.2026 24.08.2026 Отпуск
    """
    if not message_from_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = (message.text or "").split(maxsplit=3)

    if len(parts) < 4:
        await message.answer(
            "Используйте формат:\n"
            "<code>/close 10.08.2026 24.08.2026 Отпуск</code>\n\n"
            "Для одного дня укажите одну дату дважды."
        )
        return

    try:
        start_date = parse_admin_date(parts[1])
        end_date = parse_admin_date(parts[2])
    except ValueError:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ.\n\n"
            "Пример:\n"
            "<code>/close 10.08.2026 24.08.2026 Отпуск</code>"
        )
        return

    reason = parts[3].strip()

    if end_date < start_date:
        await message.answer(
            "Дата окончания не может быть раньше даты начала."
        )
        return

    if not 2 <= len(reason) <= 100:
        await message.answer(
            "Причина должна содержать от 2 до 100 символов."
        )
        return

    conflicting_bookings = get_bookings_in_period(
        start_date,
        end_date,
    )

    if conflicting_bookings:
        lines = []

        for iso_date, appointment_time, client_name in conflicting_bookings[:10]:
            lines.append(
                f"• {format_date(iso_date)} в "
                f"{html.escape(appointment_time)} — "
                f"{html.escape(client_name)}"
            )

        extra_count = len(conflicting_bookings) - len(lines)
        extra_text = (
            f"\nЕщё записей: {extra_count}."
            if extra_count > 0
            else ""
        )

        await message.answer(
            "Период не закрыт: внутри уже есть записи.\n\n"
            + "\n".join(lines)
            + extra_text
            + "\n\nСначала свяжитесь с клиентами "
              "и перенесите записи."
        )
        return

    closure_id = add_closure(
        start_date=start_date,
        end_date=end_date,
        reason=reason,
    )

    await message.answer(
        "✅ Нерабочий период добавлен.\n\n"
        f"<b>Номер:</b> #{closure_id}\n"
        f"<b>С:</b> {format_date_value(start_date)}\n"
        f"<b>По:</b> {format_date_value(end_date)}\n"
        f"<b>Причина:</b> {html.escape(reason)}\n\n"
        "Эти даты теперь отмечены знаком ×."
    )


@dp.message(Command("closures"))
async def closures_handler(message: Message) -> None:
    """Показывает текущие и будущие нерабочие периоды."""
    if not message_from_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    closures = get_future_closures()

    if not closures:
        await message.answer(
            "Добавленных отпусков и больничных пока нет.\n\n"
            "Еженедельное расписание: /schedule"
        )
        return

    lines = ["<b>Нерабочие периоды</b>\n"]

    for closure_id, start_iso, end_iso, reason in closures:
        lines.append(
            f"#{closure_id}: "
            f"{format_date(start_iso)}–{format_date(end_iso)} "
            f"— {html.escape(reason)}"
        )

    lines.append(
        "\nЧтобы удалить период:\n"
        "<code>/open НОМЕР</code>"
    )

    await message.answer("\n".join(lines))


@dp.message(Command("open"))
async def open_period_handler(message: Message) -> None:
    """Удаляет нерабочий период по его номеру."""
    if not message_from_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Укажите номер периода.\n\n"
            "Пример: <code>/open 3</code>\n"
            "Список: /closures"
        )
        return

    closure_id = int(parts[1])

    if delete_closure(closure_id):
        await message.answer(
            f"✅ Нерабочий период #{closure_id} удалён."
        )
    else:
        await message.answer(
            "Период с таким номером не найден.\n"
            "Проверьте список командой /closures."
        )



@dp.message(F.text == "📝 Записаться")
async def appointment_start(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()
    await state.set_state(Appointment.name)

    await message.answer(
        "Как вас зовут?\n\n"
        "Напишите имя одним сообщением.",
        reply_markup=cancel_menu,
    )


@dp.message(Appointment.name)
async def appointment_name(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.text:
        await message.answer(
            "Пожалуйста, напишите имя текстом."
        )
        return

    name = message.text.strip()

    if not 2 <= len(name) <= 60:
        await message.answer(
            "Имя должно содержать от 2 до 60 символов."
        )
        return

    await state.update_data(
        name=name,
        editing_datetime=False,
    )
    await state.set_state(Appointment.appointment_date)

    await ask_for_date(message)


@dp.callback_query(
    Appointment.appointment_date,
    F.data.startswith("cal:"),
)
async def calendar_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    parts = callback.data.split(":")

    if parts[1] == "noop":
        await callback.answer()
        return

    if parts[1] == "nav":
        year = int(parts[2])
        month = int(parts[3])

        await callback.message.edit_reply_markup(
            reply_markup=build_calendar(year, month)
        )
        await callback.answer()
        return

    if parts[1] == "closed":
        iso_date = parts[2]
        reason = get_closure_reason(iso_date) or "Нерабочий день"

        await callback.answer(
            f"{format_date(iso_date)}: {reason}.",
            show_alert=True,
        )
        return

    if parts[1] == "full":
        iso_date = parts[2]

        await callback.answer(
            f"{format_date(iso_date)}: свободного времени нет.",
            show_alert=True,
        )
        return

    if parts[1] != "day":
        await callback.answer()
        return

    iso_date = parts[2]
    selected_date = date.fromisoformat(iso_date)

    if selected_date < date.today():
        await callback.answer(
            "Нельзя выбрать прошедшую дату.",
            show_alert=True,
        )
        return

    closure_reason = get_closure_reason(iso_date)

    if closure_reason is not None:
        await callback.answer(
            f"Специалист не работает: {closure_reason}.",
            show_alert=True,
        )
        return

    time_keyboard = build_time_keyboard(iso_date)

    if time_keyboard is None:
        await callback.answer(
            "На эту дату свободного времени уже нет. "
            "Выберите другой день.",
            show_alert=True,
        )
        return

    await state.update_data(appointment_date=iso_date)
    await state.set_state(Appointment.appointment_time)

    await callback.message.edit_text(
        f"Вы выбрали дату: "
        f"<b>{format_date(iso_date)}</b>"
    )

    await callback.message.answer(
        "Теперь выберите свободное время:",
        reply_markup=time_keyboard,
    )

    await callback.answer()


@dp.message(Appointment.appointment_date)
async def appointment_date_invalid(
    message: Message,
) -> None:
    await message.answer(
        "Выберите дату кнопкой в календаре выше."
    )


@dp.callback_query(
    Appointment.appointment_time,
    F.data.startswith("time:"),
)
async def appointment_time_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    selected_time = callback.data.split(":", 1)[1]

    try:
        selected_time = parse_time(selected_time)
    except ValueError:
        await callback.answer(
            "Некорректное время.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    iso_date = str(data.get("appointment_date", ""))

    if not iso_date:
        await state.set_state(Appointment.appointment_date)
        await callback.answer(
            "Сначала выберите дату.",
            show_alert=True,
        )
        return

    closure_reason = get_closure_reason(iso_date)

    if closure_reason is not None:
        selected_date = date.fromisoformat(iso_date)
        await state.set_state(Appointment.appointment_date)

        await callback.message.edit_text(
            "Выбранная дата стала недоступна: "
            f"{html.escape(closure_reason)}."
        )
        await callback.message.answer(
            "Выберите другую дату:",
            reply_markup=build_calendar(
                selected_date.year,
                selected_date.month,
            ),
        )
        await callback.answer()
        return

    if not is_slot_available(iso_date, selected_time):
        updated_keyboard = build_time_keyboard(iso_date)

        if updated_keyboard is not None:
            await callback.message.edit_reply_markup(
                reply_markup=updated_keyboard
            )
            await callback.answer(
                "Это время только что заняли. "
                "Выберите другое.",
                show_alert=True,
            )
            return

        selected_date = date.fromisoformat(iso_date)
        await state.set_state(Appointment.appointment_date)

        await callback.message.edit_text(
            "На выбранную дату свободного времени "
            "больше нет."
        )
        await callback.message.answer(
            "Выберите другую дату:",
            reply_markup=build_calendar(
                selected_date.year,
                selected_date.month,
            ),
        )
        await callback.answer()
        return

    await state.update_data(
        appointment_time=selected_time
    )
    data = await state.get_data()

    await callback.message.edit_text(
        f"Вы выбрали время: <b>{selected_time}</b>"
    )

    if data.get("editing_datetime"):
        await state.update_data(editing_datetime=False)
        await state.set_state(Appointment.confirm)

        updated_data = await state.get_data()

        await callback.message.answer(
            make_summary(updated_data),
            reply_markup=confirm_menu,
        )

        await callback.answer()
        return

    await state.set_state(Appointment.contact)

    await callback.message.answer(
        "Оставьте контакт для связи.\n\n"
        "Можно нажать кнопку с номером телефона "
        "или написать Telegram, телефон либо почту.",
        reply_markup=contact_menu,
    )

    await callback.answer()


@dp.message(Appointment.appointment_time)
async def appointment_time_invalid(
    message: Message,
) -> None:
    await message.answer(
        "Выберите время кнопкой выше."
    )


@dp.message(Appointment.contact)
async def appointment_contact(
    message: Message,
    state: FSMContext,
) -> None:
    if message.contact:
        contact = message.contact.phone_number
    elif message.text:
        contact = message.text.strip()
    else:
        await message.answer(
            "Отправьте контакт кнопкой "
            "или напишите его текстом."
        )
        return

    if not 4 <= len(contact) <= 120:
        await message.answer(
            "Контакт должен содержать "
            "от 4 до 120 символов."
        )
        return

    await state.update_data(contact=contact)
    await state.set_state(Appointment.topic)

    await message.answer(
        "Кратко опишите тему обращения.\n\n"
        "Не указывайте паспортные данные, пароли "
        "и другую секретную информацию.\n\n"
        "Этот шаг можно пропустить.",
        reply_markup=topic_menu,
    )


@dp.message(Appointment.topic)
async def appointment_topic(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.text:
        await message.answer(
            "Напишите тему текстом "
            "или нажмите «Пропустить»."
        )
        return

    topic = message.text.strip()

    if topic == "⏭ Пропустить":
        topic = "Не указана"
    elif len(topic) > 700:
        await message.answer(
            "Сократите описание до 700 символов."
        )
        return

    await state.update_data(topic=topic)
    await state.set_state(Appointment.confirm)

    data = await state.get_data()

    await message.answer(
        make_summary(data),
        reply_markup=confirm_menu,
    )


@dp.message(
    Appointment.confirm,
    F.text == "📅 Изменить дату и время",
)
async def appointment_edit_datetime(
    message: Message,
    state: FSMContext,
) -> None:
    await state.update_data(editing_datetime=True)
    await state.set_state(Appointment.appointment_date)

    await ask_for_date(message)


@dp.message(
    Appointment.confirm,
    F.text == "✏️ Заполнить заново",
)
async def appointment_restart(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()
    await state.set_state(Appointment.name)

    await message.answer(
        "Начнём заново.\n\nКак вас зовут?",
        reply_markup=cancel_menu,
    )


@dp.message(
    Appointment.confirm,
    F.text == "✅ Подтвердить",
)
async def appointment_confirm(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    data = await state.get_data()
    user = message.from_user

    if user and user.username:
        username = f"@{user.username}"
    else:
        username = "не указан"

    user_id = user.id if user else None

    selected_iso_date = str(data.get("appointment_date", ""))
    closure_reason = (
        get_closure_reason(selected_iso_date)
        if selected_iso_date
        else "Дата не выбрана"
    )

    if closure_reason is not None:
        await message.answer(
            "Эта дата стала недоступна: "
            f"{html.escape(closure_reason)}.\n\n"
            "Нажмите «Изменить дату и время» "
            "и выберите другой день.",
            reply_markup=confirm_menu,
        )
        return

    selected_time = str(data.get("appointment_time", ""))

    if not is_slot_available(
        selected_iso_date,
        selected_time,
    ):
        await message.answer(
            "Выбранное время стало недоступно после "
            "изменения расписания или уже занято.\n\n"
            "Нажмите «Изменить дату и время» "
            "и выберите свободный вариант.",
            reply_markup=confirm_menu,
        )
        return

    # Слот сохраняется только на этом этапе.
    # Ограничение UNIQUE защищает даже от одновременного
    # подтверждения двумя пользователями.
    booking_saved = save_booking(
        data=data,
        telegram_user_id=user_id,
        telegram_username=username,
    )

    if not booking_saved:
        await message.answer(
            "К сожалению, выбранное время только что "
            "занял другой клиент.\n\n"
            "Нажмите «Изменить дату и время» "
            "и выберите свободный вариант.",
            reply_markup=confirm_menu,
        )
        return

    summary = make_summary(data).replace(
        "<b>Проверьте заявку</b>\n\n",
        "",
    )

    admin_text = (
        "<b>Новая заявка на консультацию</b>\n\n"
        f"{summary}\n\n"
        f"<b>Telegram:</b> "
        f"{html.escape(username)}\n"
        f"<b>ID пользователя:</b> "
        f"<code>{user_id if user_id is not None else 'неизвестен'}</code>"
    )

    sent_to_admin = False

    if ADMIN_ID is not None:
        try:
            await bot.send_message(
                ADMIN_ID,
                admin_text,
            )
            sent_to_admin = True
        except Exception:
            logger.exception(
                "Не удалось отправить заявку "
                "администратору"
            )

    await state.clear()

    if sent_to_admin:
        result_text = (
            "✅ <b>Запись подтверждена</b>\n\n"
            "Выбранные дата и время теперь заняты. "
            "Специалист свяжется с вами."
        )
    else:
        result_text = (
            "✅ <b>Запись сохранена</b>\n\n"
            "Но уведомление администратору не отправилось. "
            "Проверьте ADMIN_ID в файле .env."
        )

    await message.answer(
        result_text,
        reply_markup=main_menu,
    )


@dp.message(Appointment.confirm)
async def appointment_confirm_invalid(
    message: Message,
) -> None:
    await message.answer(
        "Выберите «Подтвердить», "
        "«Изменить дату и время», "
        "«Заполнить заново» или «Отмена».",
        reply_markup=confirm_menu,
    )



@dp.message()
async def unknown_handler(message: Message) -> None:
    await message.answer(
        "Я не понял сообщение.\n\n"
        "Выберите действие с помощью кнопок меню.",
        reply_markup=main_menu,
    )


async def main() -> None:
    init_db()
    logger.info("Bot version: %s", APP_VERSION)

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не заполнен. "
            "Добавьте токен в файл .env."
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
