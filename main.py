import asyncio
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Any

import aiosqlite
import asyncpg
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, User, BotCommand, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# =========================
# Konfiguracja
# =========================


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str
    database_url: Optional[str]
    admin_ids: set[int]


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "7599311935:AAEEgwCmqUxd6SMKRkulK2-o4MDqb3SLhtE").strip()
    if not token:
        raise RuntimeError("Brak BOT_TOKEN w zmiennych środowiskowych.")
    admin_ids = {
        int(value)
        for value in os.getenv("ADMIN_IDS", "6498309404, 7541441567").split(",")
        if value.strip().isdigit()
    }
    database_url = os.getenv("DATABASE_URL")
    return Config(bot_token=token, db_path="bot.db", database_url=database_url, admin_ids=admin_ids)


config = load_config()


# =========================
# Baza danych (Adapter SQLite / Postgres)
# =========================

class DB:
    _pool = None

    @classmethod
    async def connect(cls):
        if config.database_url:
            # PostgreSQL connection
            if not cls._pool:
                cls._pool = await asyncpg.create_pool(config.database_url)
        else:
            # SQLite connection (managed per query usually in aiosqlite, or single connection)
            pass

    @classmethod
    async def execute(cls, sql: str, params: tuple = (), fetchone: bool = False, fetchall: bool = False, commit: bool = False) -> Any:
        if config.database_url:
            # PostgreSQL logic
            param_counter = iter(range(1, 100))
            pg_sql = re.sub(r'\?', lambda _: f"${next(param_counter)}", sql)
            
            pg_sql = pg_sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            pg_sql = pg_sql.replace("OR IGNORE", "ON CONFLICT DO NOTHING")
            
            async with cls._pool.acquire() as conn:
                if fetchone:
                    return await conn.fetchrow(pg_sql, *params)
                elif fetchall:
                    return await conn.fetch(pg_sql, *params)
                else:
                    status = await conn.execute(pg_sql, *params)
                    # Extract row count from status string (e.g., "UPDATE 1", "DELETE 1", "INSERT 0 1")
                    try:
                        return int(status.split()[-1])
                    except (ValueError, IndexError):
                        return 0
        else:
            # SQLite logic
            async with aiosqlite.connect(config.db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                
                cursor = await db.execute(sql, params)
                if fetchone:
                    res = await cursor.fetchone()
                    await cursor.close()
                    if commit: await db.commit()
                    return res
                elif fetchall:
                    res = await cursor.fetchall()
                    await cursor.close()
                    if commit: await db.commit()
                    return res
                else:
                    last_id = cursor.lastrowid
                    row_count = cursor.rowcount
                    await cursor.close()
                    if commit: await db.commit()
                    return last_id if "INSERT" in sql.upper() and "RETURNING" not in sql.upper() else row_count


async def init_db() -> None:
    await DB.connect()
    
    # Tabela stock
    await DB.execute(
        """
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            quantity INTEGER NOT NULL CHECK(quantity >= 0),
            price REAL NOT NULL DEFAULT 0.0
        )
        """, commit=True
    )
    
    # Migracja kolumny price - w PG ALTER TABLE działa inaczej, ale IF NOT EXISTS w kolumnach jest trudne
    # Proste obejście: ignorujemy błąd "column exists"
    try:
        await DB.execute("ALTER TABLE stock ADD COLUMN price REAL NOT NULL DEFAULT 0.0", commit=True)
    except Exception:
        pass

    # Tabela variants
    await DB.execute(
        """
        CREATE TABLE IF NOT EXISTS variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity >= 0),
            FOREIGN KEY(product_id) REFERENCES stock(id) ON DELETE CASCADE,
            UNIQUE(product_id, name)
        )
        """, commit=True
    )

    # Migracja starych stocków
    existing_stocks = await DB.execute("SELECT id, quantity FROM stock WHERE quantity > 0", fetchall=True)
    
    for row in existing_stocks:
        # asyncpg zwraca Record, aiosqlite tuple/Row. Record działa jak dict i tuple.
        p_id = row[0]
        p_qty = row[1]
        
        has_variants = await DB.execute("SELECT 1 FROM variants WHERE product_id = ?", (p_id,), fetchone=True)
        
        if not has_variants:
            # ON CONFLICT składnia jest taka sama w PG i SQLite dla standardu
            await DB.execute(
                "INSERT INTO variants (product_id, name, quantity) VALUES (?, 'Domyślny', ?) ON CONFLICT DO NOTHING",
                (p_id, p_qty), commit=True
            )

    # Tabela orders
    await DB.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            username TEXT,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            total_price REAL NOT NULL,
            delivery_method TEXT NOT NULL,
            address TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """, commit=True
    )

    # Migracja user_id na BIGINT dla Postgresa (jeśli tabela już istnieje ze zwykłym INTEGER)
    if config.database_url:
        try:
            await DB.execute("ALTER TABLE orders ALTER COLUMN user_id TYPE BIGINT", commit=True)
        except Exception:
            pass


async def upsert_stock(name: str, price: float) -> int:
    # RETURNING id działa w obu (SQLite od 3.35, Postgres standard)
    row = await DB.execute(
        """
        INSERT INTO stock (name, quantity, price)
        VALUES (?, 0, ?)
        ON CONFLICT(name) DO UPDATE SET
            price = excluded.price
        RETURNING id
        """,
        (name, price), fetchone=True, commit=True
    )
    return row[0]


async def upsert_variant(product_id: int, variant_name: str, quantity: int) -> None:
    await DB.execute(
        """
        INSERT INTO variants (product_id, name, quantity)
        VALUES (?, ?, ?)
        ON CONFLICT(product_id, name) DO UPDATE SET
            quantity = variants.quantity + excluded.quantity
        """,
        (product_id, variant_name, quantity), commit=True
    )


async def fetch_products() -> list[tuple[int, str, int, float]]:
    rows = await DB.execute(
        """
        SELECT s.id, s.name, COALESCE(SUM(v.quantity), 0) as total_qty, s.price
        FROM stock s
        LEFT JOIN variants v ON s.id = v.product_id
        GROUP BY s.id
        HAVING COALESCE(SUM(v.quantity), 0) > 0
        ORDER BY s.name
        """, fetchall=True
    )
    # Convert asyncpg Records to tuples if needed, but they are iterable so it's fine
    return rows


async def fetch_product_variants(product_id: int) -> list[tuple[int, str, int]]:
    return await DB.execute(
        "SELECT id, name, quantity FROM variants WHERE product_id = ? AND quantity > 0 ORDER BY name",
        (product_id,), fetchall=True
    )


async def fetch_variant(variant_id: int) -> Optional[tuple[int, str, int, int, str, float]]:
    return await DB.execute(
        """
        SELECT v.id, v.name, v.quantity, s.id, s.name, s.price
        FROM variants v
        JOIN stock s ON v.product_id = s.id
        WHERE v.id = ?
        """,
        (variant_id,), fetchone=True
    )


async def fetch_product_variants_admin(product_id: int) -> list[tuple[int, str, int]]:
    return await DB.execute(
        "SELECT id, name, quantity FROM variants WHERE product_id = ? ORDER BY name",
        (product_id,), fetchall=True
    )


async def update_variant_name(variant_id: int, new_name: str) -> bool:
    try:
        row_count = await DB.execute(
            "UPDATE variants SET name = ? WHERE id = ?", (new_name, variant_id), commit=True
        )
        return row_count == 1
    except Exception:
        return False


async def delete_variant(variant_id: int) -> bool:
    row_count = await DB.execute("DELETE FROM variants WHERE id = ?", (variant_id,), commit=True)
    return row_count == 1


async def decrement_variant_stock(variant_id: int, quantity: int) -> bool:
    row_count = await DB.execute(
        "UPDATE variants SET quantity = quantity - ? WHERE id = ? AND quantity >= ?",
        (quantity, variant_id, quantity), commit=True
    )
    return row_count == 1


async def create_order(
    user_id: int,
    username: str,
    product_name: str,
    quantity: int,
    total_price: float,
    delivery_method: str,
    address: Optional[str],
) -> int:
    row = await DB.execute(
        """
        INSERT INTO orders (user_id, username, product_name, quantity, total_price, delivery_method, address, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        RETURNING id
        """,
        (user_id, username, product_name, quantity, total_price, delivery_method, address),
        fetchone=True, commit=True
    )
    return row[0]


async def fetch_order_details(order_id: int) -> Optional[tuple[int, str, str, int, float, str, str, str]]:
    return await DB.execute(
        """
        SELECT id, username, product_name, quantity, total_price, delivery_method, address, status
        FROM orders
        WHERE id = ?
        """,
        (order_id,), fetchone=True
    )


async def update_order_status(order_id: int, status: str) -> Optional[int]:
    row = await DB.execute(
        "UPDATE orders SET status = ? WHERE id = ? RETURNING user_id",
        (status, order_id),
        fetchone=True, commit=True
    )
    return row[0] if row else None


async def has_confirmed_orders(user_id: int) -> bool:
    row = await DB.execute(
        "SELECT 1 FROM orders WHERE user_id = ? AND status = 'confirmed' LIMIT 1",
        (user_id,), fetchone=True
    )
    return row is not None


async def get_user_stats(user_id: int) -> tuple[int, float]:
    row = await DB.execute(
        """
        SELECT SUM(quantity), SUM(total_price)
        FROM orders
        WHERE user_id = ? AND status = 'confirmed'
        """,
        (user_id,), fetchone=True
    )
    return (row[0] or 0, row[1] or 0.0)


async def get_all_users() -> list[tuple[str, int]]:
    return await DB.execute(
        "SELECT DISTINCT username, user_id FROM orders WHERE username IS NOT NULL",
        fetchall=True
    )


async def get_pending_orders() -> list[tuple[int, str, str, int, float]]:
    return await DB.execute(
        "SELECT id, username, product_name, quantity, total_price FROM orders WHERE status = 'pending'",
        fetchall=True
    )


async def get_order_history() -> list[tuple[int, str, str, int, str, float, str]]:
    return await DB.execute(
        """
        SELECT id, username, product_name, quantity, status, total_price, delivery_method 
        FROM orders 
        WHERE status != 'pending' 
        ORDER BY created_at DESC 
        LIMIT 10
        """,
        fetchall=True
    )


async def fetch_product_simple(product_id: int) -> Optional[tuple[int, str, float]]:
    return await DB.execute(
        "SELECT id, name, price FROM stock WHERE id = ?", (product_id,), fetchone=True
    )


async def delete_product(product_id: int) -> bool:
    row_count = await DB.execute("DELETE FROM stock WHERE id = ?", (product_id,), commit=True)
    return row_count == 1


async def update_product_quantity(product_id: int, new_quantity: int) -> bool:
    row_count = await DB.execute(
        "UPDATE stock SET quantity = ? WHERE id = ?", (new_quantity, product_id), commit=True
    )
    return row_count == 1


async def fetch_all_products_admin() -> list[tuple[int, str, int, float]]:
    return await DB.execute(
        """
        SELECT s.id, s.name, COALESCE(SUM(v.quantity), 0) as total_qty, s.price
        FROM stock s
        LEFT JOIN variants v ON s.id = v.product_id
        GROUP BY s.id
        ORDER BY s.name
        """,
        fetchall=True
    )


# =========================
# Stany FSM
# =========================


class AdminStates(StatesGroup):
    adding_stock = State()
    managing_stock = State()
    editing_quantity = State()
    renaming_variant = State()



class OrderStates(StatesGroup):
    choosing_product = State()
    choosing_variant = State()
    entering_quantity = State()
    choosing_delivery = State()
    choosing_shipping = State()
    choosing_dpd_option = State()
    entering_address = State()
    choosing_pickup_city = State()
    entering_phone = State()
    choosing_payment = State()
    confirming_order = State()


# =========================
# Routery
# =========================


admin_router = Router()
user_router = Router()
order_router = Router()


# =========================
# Klawiatury
# =========================


def main_menu_keyboard(is_admin: bool = False, show_profile: bool = False) -> ReplyKeyboardBuilder:
    builder = ReplyKeyboardBuilder()
    builder.button(text="🛍️ Kupuję")
    builder.button(text="📣 Reklamacja")
    if show_profile:
        builder.button(text="🆔 Mój profil")
    if is_admin:
        builder.button(text="🔧 Panel Admin")
    builder.adjust(2)
    return builder


def admin_menu_keyboard() -> ReplyKeyboardBuilder:
    builder = ReplyKeyboardBuilder()
    builder.button(text="🏭 Zarządzaj stockiem")
    builder.button(text="📇 Zarządzaj użytkownikami")
    builder.button(text="📨 Zarządzaj zamówieniami")
    builder.button(text="🕰️ Historia zamówień")
    builder.button(text="🔙 Powrót")
    builder.adjust(1)
    return builder


def admin_stock_actions_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Dodaj nowy produkt", callback_data="admin_stock:add")
    builder.button(text="🔢 Dodaj/Edytuj warianty", callback_data="admin_stock:edit")
    builder.button(text="✏️ Zmień nazwę wariantu", callback_data="admin_stock:rename_variant")
    builder.button(text="🗑️ Usuń wariant", callback_data="admin_stock:delete_variant")
    builder.button(text="💥 Usuń produkt", callback_data="admin_stock:delete")
    builder.adjust(1)
    return builder


def admin_products_keyboard(products: Iterable[tuple[int, str, int, float]], action: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for product_id, name, quantity, price in products:
        builder.button(
            text=f"🏷️ {name} ({quantity})", callback_data=f"admin_product:{action}:{product_id}"
        )
    builder.adjust(1)
    return builder


def products_keyboard(products: Iterable[tuple[int, str, int, float]]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for product_id, name, quantity, price in products:
        builder.button(
            text=f"🏷️ {name} ({price} PLN)", callback_data=f"product:{product_id}"
        )
    builder.adjust(1)
    return builder


def variants_reply_keyboard(variants: Iterable[tuple[int, str, int]]) -> ReplyKeyboardBuilder:
    builder = ReplyKeyboardBuilder()
    for var_id, name, quantity in variants:
        builder.button(text=f"🔹 {name} ({quantity})")
    builder.button(text="🔙 Anuluj")
    builder.adjust(1)
    return builder


def delivery_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🤝 Odbiór osobisty", callback_data="delivery:pickup")
    builder.button(text="🚚 Wysyłka", callback_data="delivery:ship")
    builder.adjust(2)
    return builder


def shipping_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚚 DPD", callback_data="shipping:DPD")
    builder.button(text="📮 InPost (+12 PLN)", callback_data="shipping:InPost")
    builder.adjust(2)
    return builder


def dpd_options_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏪 Do punktu (+6 PLN)", callback_data="dpd_option:point")
    builder.button(text="📦 Do automatu (+10 PLN)", callback_data="dpd_option:machine")
    builder.adjust(1)
    return builder


def pickup_cities_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏙️ Warszawa", callback_data="pickup_city:Warszawa")
    builder.adjust(1)
    return builder


def payment_keyboard(allow_cash: bool = True) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="💸 BLIK", callback_data="payment:BLIK")
    if allow_cash:
        builder.button(text="💵 Gotówka", callback_data="payment:Gotówka")
    builder.adjust(2)
    return builder


def admin_order_keyboard(order_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🆗 Potwierdź", callback_data=f"order:confirm:{order_id}")
    builder.button(text="⛔ Odrzuć", callback_data=f"order:reject:{order_id}")
    builder.adjust(2)
    return builder


# =========================
# Handlery ogólne
# =========================


@user_router.message(CommandStart())
async def start_command(message: Message) -> None:
    if message.from_user is None:
        return
    is_admin = message.from_user.id in config.admin_ids
    show_profile = await has_confirmed_orders(message.from_user.id)
    
    await message.answer(
        "Wybierz opcję z menu:",
        reply_markup=main_menu_keyboard(is_admin, show_profile).as_markup(resize_keyboard=True),
    )


@user_router.message(Command("pomoc"))
async def help_command(message: Message) -> None:
    await message.answer(
        "ℹ️ *Pomoc*\n\n"
        "Ten bot umożliwia zamawianie produktów.\n\n"
        "🛍️ *Kupuję* – Przeglądaj dostępne produkty i składaj zamówienia.\n"
        "📣 *Reklamacja* – Zgłoś problem z zamówieniem.\n"
        "🆔 *Mój profil* – Sprawdź statystyki swoich zamówień (widoczne po pierwszym zamówieniu).\n\n"
        "W razie problemów skontaktuj się z administratorem.",
        parse_mode="Markdown"
    )


@user_router.message(F.text == "🔧 Panel Admin")
async def admin_command(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        await message.answer("⛔ Brak uprawnień do panelu admina.")
        return
    await state.clear()
    await message.answer(
        "Panel administratora:",
        reply_markup=admin_menu_keyboard().as_markup(resize_keyboard=True),
    )


@user_router.message(F.text == "🔙 Powrót")
async def back_to_main(message: Message) -> None:
    await start_command(message)


@user_router.message(F.text == "🆔 Mój profil")
async def my_profile(message: Message) -> None:
    if message.from_user is None:
        return
    stats = await get_user_stats(message.from_user.id)
    await message.answer(
        f"🆔 Twój profil:\n🛍️ Kupione produkty: {stats[0]} szt.\n💰 Wydana kwota: {stats[1]:.2f} PLN"
    )


@user_router.message(F.text == "📣 Reklamacja")
async def complaint_flow(message: Message) -> None:
    await message.answer("📝 Napisz proszę szczegóły reklamacji na czacie.")


# =========================
# Panel administratora
# =========================


@admin_router.message(F.text == "🏭 Zarządzaj stockiem")
async def manage_stock_menu(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        return
    await message.answer(
        "Wybierz akcję:",
        reply_markup=admin_stock_actions_keyboard().as_markup(),
    )


@admin_router.callback_query(F.data.startswith("admin_stock:"))
async def admin_stock_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    
    if action == "add":
        await state.set_state(AdminStates.adding_stock)
        await callback.message.answer(
            "Wpisz dane w formacie: Nazwa produktu | Cena (np. Jednorazówka | 25.50)"
        )
        await callback.answer()
        return

    products = await fetch_all_products_admin()
    if not products:
        await callback.message.answer("📭 Brak produktów w bazie.")
        await callback.answer()
        return

    if action == "edit":
        # W nowej logice edycja stocku to dodanie wariantu do istniejącego produktu
        await callback.message.answer(
            "Wybierz produkt, do którego chcesz dodać wariant/stock:",
            reply_markup=admin_products_keyboard(products, "add_variant").as_markup()
        )
    elif action == "delete":
        await callback.message.answer(
            "Wybierz produkt do usunięcia (usunie też wszystkie warianty):",
            reply_markup=admin_products_keyboard(products, "delete").as_markup()
        )
    elif action == "delete_variant":
        await callback.message.answer(
            "Wybierz produkt, z którego chcesz usunąć wariant:",
            reply_markup=admin_products_keyboard(products, "select_product_delete_variant").as_markup()
        )
    elif action == "rename_variant":
        await callback.message.answer(
            "Wybierz produkt, w którym chcesz zmienić nazwę wariantu:",
            reply_markup=admin_products_keyboard(products, "select_product_rename_variant").as_markup()
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_product:"))
async def admin_product_selection(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    product_id = int(parts[2])
    
    if action == "delete":
        success = await delete_product(product_id)
        if success:
            await callback.message.edit_text("💥 Produkt został usunięty.")
        else:
            await callback.message.answer("⛔ Nie udało się usunąć produktu.")
    elif action == "add_variant":
        product = await fetch_product_simple(product_id)
        if not product:
            await callback.message.answer("⛔ Produkt nie istnieje.")
            await callback.answer()
            return
        await state.update_data(product_id=product_id, product_name=product[1])
        await state.set_state(AdminStates.editing_quantity) # Reusing state name but logic differs
        await callback.message.answer(
            f"Wybrano produkt: {product[1]}\n"
            "Podaj wariant i ilość w formacie: Nazwa wariantu | Ilość\n"
            "(np. Arbuz | 100)"
        )
    elif action in ("select_product_delete_variant", "select_product_rename_variant"):
        product = await fetch_product_simple(product_id)
        if not product:
            await callback.message.answer("⛔ Produkt nie istnieje.")
            await callback.answer()
            return
        
        variants = await fetch_product_variants_admin(product_id)
        if not variants:
            await callback.message.answer("⛔ Ten produkt nie ma żadnych wariantów.")
            await callback.answer()
            return
            
        builder = InlineKeyboardBuilder()
        next_action = "delete_variant_confirm" if action == "select_product_delete_variant" else "rename_variant_input"
        
        for v_id, v_name, v_qty in variants:
            builder.button(
                text=f"{v_name} ({v_qty})",
                callback_data=f"admin_variant:{next_action}:{v_id}"
            )
        builder.adjust(1)
        
        await callback.message.answer(
            f"Wybierz wariant produktu {product[1]}:",
            reply_markup=builder.as_markup()
        )
    elif action == "delete_variant_confirm":
        # To handle direct variant deletion confirmation if we skip selection step? No, logic above uses admin_variant prefix
        pass

    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_variant:"))
async def admin_variant_action(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    variant_id = int(parts[2])
    
    if action == "delete_variant_confirm":
        variant = await fetch_variant(variant_id)
        if variant:
            await delete_variant(variant_id)
            await callback.message.edit_text(f"💥 Usunięto wariant: {variant[1]}")
        else:
            await callback.message.answer("⛔ Wariant nie istnieje.")
            
    elif action == "rename_variant_input":
        variant = await fetch_variant(variant_id)
        if not variant:
            await callback.message.answer("⛔ Wariant nie istnieje.")
            return
            
        await state.update_data(variant_id=variant_id, old_name=variant[1])
        await state.set_state(AdminStates.renaming_variant)
        await callback.message.answer(
            f"Podaj nową nazwę dla wariantu '{variant[1]}':"
        )
    
    await callback.answer()


@admin_router.message(AdminStates.renaming_variant)
async def admin_save_renamed_variant(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    variant_id = data.get("variant_id")
    old_name = data.get("old_name")
    new_name = (message.text or "").strip()
    
    if not new_name:
        await message.answer("⛔ Nazwa nie może być pusta.")
        return
        
    if await update_variant_name(variant_id, new_name):
        await message.answer(f"🆗 Zmieniono nazwę wariantu z '{old_name}' na '{new_name}'.")
    else:
        await message.answer("⛔ Nie udało się zmienić nazwy (może taka nazwa już istnieje?).")
        
    await state.clear()


@admin_router.message(AdminStates.editing_quantity)
async def admin_save_variant(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    product_id = data.get("product_id")
    product_name = data.get("product_name")
    
    text = (message.text or "").strip()
    parts = [part.strip() for part in text.split("|")]
    
    if len(parts) != 2:
         await message.answer("⛔ Nieprawidłowy format. Użyj: Nazwa wariantu | Ilość")
         return
         
    variant_name, qty_text = parts
    
    if not qty_text.isdigit():
        await message.answer("⛔ Podaj ilość jako liczbę całkowitą.")
        return
        
    quantity = int(qty_text)
    if quantity < 0:
        await message.answer("⛔ Ilość nie może być ujemna.")
        return
        
    await upsert_variant(product_id, variant_name, quantity)
    await message.answer(f"🆗 Dodano wariant dla {product_name}: {variant_name} ({quantity} szt.).")
    await state.clear()


@admin_router.message(AdminStates.adding_stock)
async def add_product_parse(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        return
    text = (message.text or "").strip()
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 2:
        await message.answer("⛔ Nieprawidłowy format. Użyj: Nazwa produktu | Cena")
        return
    name_part, price_part = parts
    
    if not name_part:
        await message.answer("⛔ Podaj nazwę produktu.")
        return
    try:
        price = float(price_part.replace(",", "."))
    except ValueError:
        await message.answer("⛔ Podaj cenę jako liczbę.")
        return
    if price < 0:
        await message.answer("⛔ Cena nie może być ujemna.")
        return

    product_id = await upsert_stock(name_part, price)
    await state.clear()
    await message.answer(
        f"🆗 Utworzono produkt: {name_part} (Cena: {price} PLN).\n"
        "Teraz przejdź do 'Zarządzaj stockiem' -> 'Edytuj ilość', aby dodać warianty (smaki)."
    )


@admin_router.message(F.text == "📇 Zarządzaj użytkownikami")
async def list_users(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        return
    users = await get_all_users()
    if not users:
        await message.answer("📭 Brak użytkowników w bazie.")
        return
    text = "📇 Użytkownicy:\n" + "\n".join([f"- {u[0]} (ID: {u[1]})" for u in users])
    await message.answer(text)


@admin_router.message(F.text == "📨 Zarządzaj zamówieniami")
async def list_pending_orders(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        return
    orders = await get_pending_orders()
    if not orders:
        await message.answer("📭 Brak oczekujących zamówień.")
        return
    text = "📨 Oczekujące zamówienia:\n"
    for o in orders:
        text += f"ID: {o[0]} | {o[1]} | {o[2]} x{o[3]} ({o[4]} PLN)\n"
    await message.answer(text)


@admin_router.message(F.text == "🕰️ Historia zamówień")
async def list_order_history(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        return
    orders = await get_order_history()
    if not orders:
        await message.answer("📭 Brak historii zamówień.")
        return
    text = "🕰️ Ostatnie zamówienia:\n\n"
    for o in orders:
        # o: id, username, product_name, quantity, status, total_price, delivery_method
        status_icon = '🆗' if o[4] == 'confirmed' else '⛔'
        text += (
            f"🔹 ID: {o[0]} | {status_icon}\n"
            f"👤 {o[1]}\n"
            f"🏷️ {o[2]} x{o[3]}\n"
            f"💰 {o[5]:.2f} PLN\n"
            f"📦 {o[6]}\n"
            "-------------------\n"
        )
    await message.answer(text)


# =========================
# Proces zamówienia
# =========================


@order_router.message(F.text == "🛍️ Kupuję")
async def start_order(message: Message, state: FSMContext) -> None:
    products = await fetch_products()
    if not products:
        await message.answer("📭 Brak dostępnych produktów.")
        return
    await state.set_state(OrderStates.choosing_product)
    await message.answer(
        "Wybierz produkt:",
        reply_markup=products_keyboard(products).as_markup(),
    )




@order_router.callback_query(OrderStates.choosing_product, F.data.startswith("product:"))
async def choose_product(callback: CallbackQuery, state: FSMContext) -> None:
    product_id = int(callback.data.split(":")[1])
    product = await fetch_product_simple(product_id)
    if not product:
        await callback.message.answer("⛔ Produkt nie istnieje.")
        await callback.answer()
        return

    variants = await fetch_product_variants(product_id)
    if not variants:
        await callback.message.answer("⛔ Brak dostępnych wariantów dla tego produktu.")
        await callback.answer()
        return

    await state.update_data(product_id=product_id, product_name=product[1], price=product[2])
    await state.set_state(OrderStates.choosing_variant)
    await callback.message.answer(
        f"Wybrano: {product[1]}\nTeraz wybierz wariant (ilość w nawiasie):",
        reply_markup=variants_reply_keyboard(variants).as_markup(resize_keyboard=True)
    )
    await callback.answer()


@order_router.message(OrderStates.choosing_variant)
async def choose_variant(message: Message, state: FSMContext) -> None:
    text = message.text
    if text == "🔙 Anuluj":
        await state.clear()
        await start_command(message)
        return

    data = await state.get_data()
    product_id = data.get("product_id")
    variants = await fetch_product_variants(product_id)
    
    selected_variant = None
    for v in variants:
        # Reconstruct the button text to compare
        btn_text = f"🔹 {v[1]} ({v[2]})"
        if text == btn_text:
            selected_variant = v
            break
            
    if not selected_variant:
        await message.answer("⛔ Wybierz wariant z klawiatury.")
        return
         
    await state.update_data(variant_id=selected_variant[0], variant_name=selected_variant[1], max_quantity=selected_variant[2])
    await state.set_state(OrderStates.entering_quantity)
    
    product_name = data.get("product_name", "Produkt")
    await message.answer(
        f"Wybrano: {product_name} - {selected_variant[1]}\nPodaj ilość (dostępne: {selected_variant[2]}):",
        reply_markup=ReplyKeyboardRemove()
    )


@order_router.message(OrderStates.entering_quantity)
async def enter_quantity(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    
    # Obsługa przypadku, gdy użytkownik ponownie kliknie przycisk wariantu (lub dwuklik)
    if text.startswith("🔹"):
        data = await state.get_data()
        product_id = data.get("product_id")
        if product_id:
            variants = await fetch_product_variants(product_id)
            for v in variants:
                btn_text = f"🔹 {v[1]} ({v[2]})"
                if text == btn_text:
                    # Aktualizujemy wybór wariantu, mimo że jesteśmy już w stanie entering_quantity
                    await state.update_data(variant_id=v[0], variant_name=v[1], max_quantity=v[2])
                    product_name = data.get("product_name", "Produkt")
                    await message.answer(
                        f"🔄 Zmieniono na: {product_name} - {v[1]}\nPodaj ilość (dostępne: {v[2]}):",
                        reply_markup=ReplyKeyboardRemove()
                    )
                    return

    if not text.isdigit():
        await message.answer("⛔ Podaj liczbę całkowitą.")
        return
    
    quantity = int(text)
    if quantity < 1:
        await message.answer("⛔ Ilość musi być większa od 0.")
        return

    data = await state.get_data()
    variant_id = data.get("variant_id")
    # Refresh variant data to check stock
    variant = await fetch_variant(variant_id)

    if not variant or variant[2] < quantity:
        await message.answer(f"⛔ Niewystarczająca ilość w magazynie (dostępne: {variant[2] if variant else 0}).")
        return

    await state.update_data(quantity=quantity)
    await state.set_state(OrderStates.choosing_delivery)
    await message.answer(
        "Wybierz metodę dostawy:",
        reply_markup=delivery_keyboard().as_markup(),
    )


@order_router.callback_query(OrderStates.choosing_delivery, F.data.startswith("delivery:"))
async def choose_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    delivery_type = callback.data.split(":")[1]
    await state.update_data(delivery_type=delivery_type)
    
    if delivery_type == "ship":
        await state.set_state(OrderStates.choosing_shipping)
        await callback.message.answer(
            "Wybierz przewoźnika:",
            reply_markup=shipping_keyboard().as_markup()
        )
    else:
        # Odbiór osobisty - wybór miasta
        await state.set_state(OrderStates.choosing_pickup_city)
        await callback.message.answer(
            "Wybierz miasto odbioru:",
            reply_markup=pickup_cities_keyboard().as_markup()
        )
    await callback.answer()


@order_router.callback_query(OrderStates.choosing_pickup_city, F.data.startswith("pickup_city:"))
async def choose_pickup_city(callback: CallbackQuery, state: FSMContext) -> None:
    city = callback.data.split(":")[1]
    await state.update_data(shipping_method=None, shipping_cost=0.0, address=f"Odbiór osobisty: {city}")
    
    await state.set_state(OrderStates.choosing_payment)
    await callback.message.answer(
        "Wybierz metodę płatności:",
        reply_markup=payment_keyboard().as_markup()
    )
    await callback.answer()


@order_router.callback_query(OrderStates.choosing_payment, F.data.startswith("payment:"))
async def choose_payment(callback: CallbackQuery, state: FSMContext) -> None:
    payment_method = callback.data.split(":")[1]
    await state.update_data(payment_method=payment_method)
    
    # Podsumowanie zamówienia
    data = await state.get_data()
    product_name = data["product_name"]
    variant_name = data["variant_name"]
    quantity = data["quantity"]
    price = data["price"]
    delivery_type = data["delivery_type"]
    shipping_method = data.get("shipping_method")
    shipping_cost = data.get("shipping_cost", 0.0)
    address = data.get("address")
    phone = data.get("phone")
    
    total_price = (quantity * price) + shipping_cost
    
    summary_text = (
        "📋 **Podsumowanie zamówienia**\n\n"
        f"🏷️ Produkt: {product_name} ({variant_name})\n"
        f"🔢 Ilość: {quantity}\n"
        f"🚚 Dostawa: {delivery_type}"
    )
    
    if shipping_method:
        summary_text += f" ({shipping_method})"
    
    summary_text += f"\n💰 Łączna kwota: {total_price:.2f} PLN\n"
    
    if address:
        summary_text += f"📬 Adres: {address}\n"
    if phone:
        summary_text += f"📞 Telefon: {phone}\n"
        
    summary_text += f"💳 Płatność: {payment_method}\n\n"
    summary_text += "Czy wszystko się zgadza?"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Tak ✅", callback_data="confirm_order:yes")
    builder.button(text="Nie ❌", callback_data="confirm_order:no")
    builder.button(text="Chcę anulować zamówienie 🚫", callback_data="confirm_order:cancel")
    builder.adjust(2, 1)
    
    await state.set_state(OrderStates.confirming_order)
    await callback.message.edit_text(summary_text, reply_markup=builder.as_markup())
    await callback.answer()


@order_router.callback_query(OrderStates.confirming_order, F.data.startswith("confirm_order:"))
async def process_order_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    
    if action == "yes":
        if callback.message and callback.from_user:
            # Przekazujemy message z callbacku, żeby finalize_order mogło odpowiedzieć
            await finalize_order(callback.message, state, callback.from_user)
    elif action == "no":
        await state.clear()
        await callback.message.edit_text("Rozumiem, że dane się nie zgadzają. Zacznijmy od nowa.\nWybierz /start aby rozpocząć.")
    elif action == "cancel":
        await state.clear()
        await callback.message.edit_text("🚫 Zamówienie zostało anulowane.")
    
    await callback.answer()


@order_router.callback_query(OrderStates.choosing_shipping, F.data.startswith("shipping:"))
async def choose_shipping_method(callback: CallbackQuery, state: FSMContext) -> None:
    shipping_method = callback.data.split(":")[1]
    
    if shipping_method == "DPD":
        await state.set_state(OrderStates.choosing_dpd_option)
        await callback.message.answer(
            "Wybierz opcję dostawy DPD:",
            reply_markup=dpd_options_keyboard().as_markup()
        )
    else:
        # InPost - koszt 12 zł
        await state.update_data(shipping_method="InPost", shipping_cost=12.0)
        await state.set_state(OrderStates.entering_address)
        await callback.message.answer("📬 Podaj adres dostawy (paczkomat, ulica itp.):")
    
    await callback.answer()


@order_router.callback_query(OrderStates.choosing_dpd_option, F.data.startswith("dpd_option:"))
async def choose_dpd_option(callback: CallbackQuery, state: FSMContext) -> None:
    option = callback.data.split(":")[1]
    
    if option == "point":
        cost = 6.0
        details = "DPD Punkt"
    else:
        cost = 10.0
        details = "DPD Automat"
        
    await state.update_data(shipping_method=details, shipping_cost=cost)
    await state.set_state(OrderStates.entering_address)
    await callback.message.answer("📬 Podaj adres dostawy (punkt/automat, miasto):")
    await callback.answer()


@order_router.message(OrderStates.entering_address)
async def enter_address(message: Message, state: FSMContext) -> None:
    address = (message.text or "").strip()
    if len(address) < 5:
        await message.answer("⛔ Adres jest za krótki.")
        return
    
    await state.update_data(address=address)
    await state.set_state(OrderStates.entering_phone)
    await message.answer("📞 Podaj numer telefonu dla kuriera (np. 123456789):")


@order_router.message(OrderStates.entering_phone)
async def enter_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if len(phone) < 9:
        await message.answer("⚠️ Numer telefonu wydaje się niepoprawny. Spróbuj ponownie.")
        return
        
    await state.update_data(phone=phone)
    await state.set_state(OrderStates.choosing_payment)
    await message.answer("💳 Wybierz metodę płatności:", reply_markup=payment_keyboard(allow_cash=False).as_markup())


@order_router.callback_query(F.data.startswith(("product:", "delivery:", "shipping:", "dpd_option:", "pickup_city:", "payment:", "confirm_order:")))
async def session_expired(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Sesja wygasła. Wybierz 'Kupuję' z menu ponownie.", show_alert=True)


async def finalize_order(message: Message, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    product_id = data["product_id"]
    product_name = data["product_name"]
    variant_id = data["variant_id"]
    variant_name = data["variant_name"]
    quantity = data["quantity"]
    price = data["price"]
    delivery_type = data["delivery_type"]
    shipping_method = data.get("shipping_method")
    shipping_cost = data.get("shipping_cost", 0.0)
    address = data.get("address")
    phone = data.get("phone")
    payment_method = data.get("payment_method")
    
    total_price = (quantity * price) + shipping_cost
    
    # Check variant stock
    variant = await fetch_variant(variant_id)
    if not variant or variant[2] < quantity:
        await message.edit_text("⛔ Niestety wybrany wariant w międzyczasie się wyprzedał.")
        await state.clear()
        return
    
    # Deduct stock from variant
    if not await decrement_variant_stock(variant_id, quantity):
        await message.edit_text("⛔ Błąd systemu podczas aktualizacji stanu.")
        await state.clear()
        return
    
    # Create order
    full_product_name = f"{product_name} ({variant_name})"
    details = f"Dostawa: {delivery_type}"
    if shipping_method:
        details += f", Przewoźnik: {shipping_method}"
    if address:
        details += f", Adres: {address}"
    if phone:
        details += f", Telefon: {phone}"
    if payment_method:
        details += f", Płatność: {payment_method}"
    
    order_id = await create_order(
        user.id, f"{user.full_name} (@{user.username or '-'})", full_product_name, quantity, total_price, details, address
    )
    
    await notify_admins(
        message.bot, order_id, user, full_product_name, quantity, total_price, details
    )
    
    await state.clear()
    await message.edit_text(
        f"🆗 Zamówienie przyjęte (ID: {order_id})!\n"
        f"🏷️ Produkt: {full_product_name}\n"
        f"🔢 Ilość: {quantity}\n"
        f"💰 Do zapłaty: {total_price:.2f} PLN\n\n"
        "Czekaj na potwierdzenie przez administratora.",
        reply_markup=main_menu_keyboard(
            is_admin=(user.id in config.admin_ids),
            show_profile=(await has_confirmed_orders(user.id))
        ).as_markup(resize_keyboard=True)
    )


async def notify_admins(
    bot, order_id: int, user: User, product_name: str, quantity: int, total_price: float, details: str
) -> None:
    msg_text = (
        f"🆕 Nowe zamówienie (ID: {order_id})!\n"
        f"🆔 Użytkownik: {user.full_name} (@{user.username or 'brak'})\n"
        f"🏷️ Produkt: {product_name}\n"
        f"🔢 Ilość: {quantity}\n"
        f"💰 Kwota: {total_price:.2f} PLN\n"
        f"📝 Szczegóły: {details}"
    )
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(
                admin_id, msg_text, reply_markup=admin_order_keyboard(order_id).as_markup()
            )
        except Exception:
            pass


@admin_router.callback_query(F.data.startswith("order:"))
async def process_order_decision(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    order_id = int(parts[2])
    
    status = "confirmed" if action == "confirm" else "rejected"
    user_id = await update_order_status(order_id, status)
    
    if user_id:
        # Pobierz szczegóły zamówienia, aby zaktualizować wiadomość admina
        order_details = await fetch_order_details(order_id)
        
        status_text = '🆗 potwierdzone' if status == 'confirmed' else '⛔ odrzucone'
        
        if order_details:
            # order_details: id, username, product_name, quantity, total_price, delivery_method, address, status
            # Używamy formatowania z notify_admins, aby zachować spójność
            new_text = (
                f"Zamówienie {order_id} zostało {status_text}.\n\n"
                f"🆔 Użytkownik: {order_details[1]}\n"
                f"🏷️ Produkt: {order_details[2]}\n"
                f"🔢 Ilość: {order_details[3]}\n"
                f"💰 Kwota: {order_details[4]:.2f} PLN\n"
                f"📝 Szczegóły: {order_details[5]}"
            )
        else:
            new_text = f"Zamówienie {order_id} zostało {status_text}."
            
        await callback.message.edit_text(new_text)
        
        # Powiadom użytkownika
        user_msg = (
            f"ℹ️ Twoje zamówienie #{order_id} zostało {status_text}."
        )
        try:
            await callback.bot.send_message(user_id, user_msg)
        except Exception:
            # Użytkownik mógł zablokować bota
            pass
    else:
        await callback.message.answer("⛔ Błąd aktualizacji statusu zamówienia.")
    
    await callback.answer()


async def keep_alive():
    url = os.getenv("WEBHOOK_URL")
    if not url:
        return
    async with ClientSession() as session:
        while True:
            try:
                # Pinguj główny URL (nie webhook endpoint, żeby nie triggerować błędów)
                # Jeśli WEBHOOK_URL to np. https://app.onrender.com, to pingujemy to.
                async with session.get(url) as resp:
                    await resp.text()
            except Exception:
                pass
            await asyncio.sleep(120)  # Ping co 2 minuty


async def main() -> None:
    bot = Bot(token=config.bot_token)
    
    # Setup commands
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Uruchom bota"),
        BotCommand(command="pomoc", description="❓ Pomoc")
    ])
    
    await init_db()
    
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(order_router)
    dp.include_router(user_router)

    # Konfiguracja Webhooka lub Pollinga
    webhook_url = os.getenv("WEBHOOK_URL")
    port = int(os.getenv("PORT", 8080))

    if webhook_url:
        # Tryb Webhook (dla Render)
        app = web.Application()
        
        # Handler dla webhooka Telegrama
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
        )
        # Rejestracja endpointu webhooka
        webhook_requests_handler.register(app, path="/webhook")
        
        # Handler dla root path (health check / keep-alive)
        async def root_handler(request):
            return web.Response(text="Bot is running correctly")
        app.router.add_get("/", root_handler)
        
        # Setup aplikacji (automatycznie dodaje on_startup/on_shutdown)
        setup_application(app, dp, bot=bot)

        # Ustawienie webhooka na starcie
        async def on_startup(app):
            await bot.set_webhook(f"{webhook_url}/webhook")
            asyncio.create_task(keep_alive())
            
        app.on_startup.append(on_startup)

        # Uruchomienie serwera
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        
        # Utrzymanie procesu przy życiu (web.TCPSite jest nieblokujące)
        await asyncio.Event().wait()
        
    else:
        # Tryb Polling (lokalnie)
        # Start dummy web server for Render (keep-alive) if needed, but locally not needed
        # But if we are on Render without WEBHOOK_URL, we still need the dummy server
        if os.getenv("RENDER"):
             app = web.Application()
             async def home(request):
                 return web.Response(text="Bot is running (Polling)")
             app.router.add_get("/", home)
             runner = web.AppRunner(app)
             await runner.setup()
             site = web.TCPSite(runner, "0.0.0.0", port)
             await site.start()
             
        # Usunięcie webhooka przed pollingiem (rozwiązuje konflikt)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
