import asyncio
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TOKEN = os.getenv("TOKEN", "").strip()
API_URL = os.getenv("API_URL", "").strip()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12.0"))
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "3.0"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip()

if not TOKEN:
    raise ValueError("❌ TOKEN no definido")
if not API_URL:
    raise ValueError("❌ API_URL no definido")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

logger = logging.getLogger("velvet_lab_bot")

REFERENCIAS: Dict[str, str] = {}

ZONAS = [
    "Delanteros",
    "Espaldas/traseros",
    "Cuellos",
    "Mangas",
    "Pretina",
    "Ruedos"
]

ESTADOS = {
    "MENU": "menu",
    "REFERENCIA": "referencia",
    "TRABAJANDO": "trabajando",
    "PAUSA": "pausa",
    "CAMBIO_CANTIDAD_CERRADA": "cambio_cantidad_cerrada",
    "CAMBIO_REF": "cambio_ref",
    "CAMBIO_CANTIDAD_NUEVA": "cambio_cantidad_nueva",
    "ZONAS": "zonas",
}

USER_CACHE: Dict[str, Dict[str, Any]] = {}
USER_CACHE_TTL = 30
REF_CACHE: Dict[str, Any] = {"expires_at": 0, "data": {}}
REF_CACHE_TTL = 120


def now_iso() -> str:
    return datetime.now().isoformat()


def get_user_id(update: Update) -> str:
    return str(update.effective_user.id).strip() if update.effective_user else ""


def menu_principal() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🟢 Iniciar turno"]], resize_keyboard=True)


def menu_trabajo() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["⏸ Pausa"],
        ["🔁 Cambio referencia"],
        ["🔴 Finalizar jornada"]
    ], resize_keyboard=True)


def menu_pausa() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["▶️ Reanudar"],
        ["🔴 Finalizar jornada"]
    ], resize_keyboard=True)


def menu_referencias() -> ReplyKeyboardMarkup:
    botones = [[f"{k} - {v}"] for k, v in REFERENCIAS.items()]
    if not botones:
        botones = [["SIN REFERENCIAS"]]
    return ReplyKeyboardMarkup(botones, resize_keyboard=True)


def menu_zonas() -> ReplyKeyboardMarkup:
    botones = [[z] for z in ZONAS]
    botones.append(["✅ Terminar selección"])
    return ReplyKeyboardMarkup(botones, resize_keyboard=True)


def parse_referencia(texto: str) -> str:
    key = texto.split(" - ")[0].strip()
    return REFERENCIAS.get(key, texto)


def is_positive_int(texto: str) -> bool:
    return texto.isdigit() and int(texto) > 0


def reset_user_state(context: ContextTypes.DEFAULT_TYPE, user_id: str, nombre: Optional[str] = None) -> None:
    context.user_data.clear()
    context.user_data["estado"] = ESTADOS["MENU"]
    context.user_data["user_id"] = user_id
    if nombre:
        context.user_data["nombre"] = nombre


def should_ignore_duplicate(context: ContextTypes.DEFAULT_TYPE, action_key: str, window_seconds: float = 1.3) -> bool:
    now_ts = time.time()
    last_key = context.user_data.get("_last_action_key")
    last_ts = context.user_data.get("_last_action_ts", 0.0)

    if last_key == action_key and (now_ts - last_ts) <= window_seconds:
        return True

    context.user_data["_last_action_key"] = action_key
    context.user_data["_last_action_ts"] = now_ts
    return False


def make_event_id(user_id: str, accion: str) -> str:
    return f"{user_id}-{accion}-{uuid.uuid4().hex[:12]}"


async def post_init(app: Application) -> None:
    timeout = httpx.Timeout(
        timeout=HTTP_TIMEOUT,
        connect=HTTP_CONNECT_TIMEOUT,
        read=HTTP_TIMEOUT,
        write=HTTP_TIMEOUT,
        pool=HTTP_TIMEOUT,
    )

    limits = httpx.Limits(
        max_connections=50,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,
    )

    app.bot_data["http"] = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        headers={"User-Agent": "velvet-lab-bot/railway-ready"},
    )

    logger.info("HTTP client inicializado")


async def post_shutdown(app: Application) -> None:
    client: Optional[httpx.AsyncClient] = app.bot_data.get("http")
    if client:
        await client.aclose()
    logger.info("HTTP client cerrado")


async def api_get(context: ContextTypes.DEFAULT_TYPE, params: Dict[str, Any]) -> Dict[str, Any]:
    client: httpx.AsyncClient = context.application.bot_data["http"]

    try:
        r = await client.get(API_URL, params=params)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {"ok": False, "error": "respuesta_invalida"}

    except httpx.ReadTimeout:
        logger.warning("ReadTimeout API params=%s", params)
        return {"ok": False, "error": "timeout"}

    except Exception as e:
        logger.exception("Error API: %s", e)
        return {"ok": False, "error": str(e)}


async def consultar_usuario(context: ContextTypes.DEFAULT_TYPE, user_id: str, force: bool = False) -> Dict[str, Any]:
    cached = USER_CACHE.get(user_id)

    if not force and cached and cached.get("expires_at", 0) > time.time():
        return cached["data"]

    data = await api_get(context, {"user": user_id})

    USER_CACHE[user_id] = {
        "expires_at": time.time() + USER_CACHE_TTL,
        "data": data
    }

    return data


async def cargar_referencias(context: ContextTypes.DEFAULT_TYPE, user_id: str, force: bool = False) -> bool:
    global REFERENCIAS

    if not force and REF_CACHE["expires_at"] > time.time() and REF_CACHE["data"]:
        REFERENCIAS = REF_CACHE["data"]
        return True

    data = await api_get(context, {
        "user": user_id,
        "accion": "get_referencias"
    })

    if data.get("ok"):
        refs = {str(x["key"]): str(x["nombre"]) for x in data.get("referencias", [])}
        REFERENCIAS = refs
        REF_CACHE["data"] = refs
        REF_CACHE["expires_at"] = time.time() + REF_CACHE_TTL
        return True

    return False


async def enviar_drive(user_id: str, context: ContextTypes.DEFAULT_TYPE, accion: str, event_id: Optional[str] = None) -> Dict[str, Any]:
    data = context.user_data

    payload = {
        "user": user_id,
        "accion": accion,
        "referencia": data.get("referencia"),
        "cantidad": data.get("cantidad"),
        "cantidad_cerrada": data.get("cantidad_cerrada"),
        "cantidad_nueva": data.get("cantidad_nueva"),
        "inicio": data.get("inicio"),
        "fin": data.get("fin"),
        "pausa": data.get("pausa_inicio"),
        "zona": data.get("zona"),
        "event_id": event_id or make_event_id(user_id, accion),
    }

    resp = await api_get(context, payload)
    logger.info("accion=%s user=%s resp=%s", accion, user_id, resp)
    return resp


async def enviar_drive_with_recovery(
    user_id: str,
    context: ContextTypes.DEFAULT_TYPE,
    accion: str,
    success_if_retry_error: Optional[str] = None,
) -> Dict[str, Any]:

    event_id = make_event_id(user_id, accion)
    resp = await enviar_drive(user_id, context, accion, event_id=event_id)

    if resp.get("ok"):
        return resp

    # ✅ Si Apps Script responde esto, la acción realmente ya quedó aplicada.
    if success_if_retry_error and resp.get("error") == success_if_retry_error:
        return {
            "ok": True,
            "recovered": True,
            "warning": success_if_retry_error
        }

    # ✅ Si hizo timeout, damos una segunda oportunidad con otro event_id.
    if resp.get("error") == "timeout":
        await asyncio.sleep(0.8)

        retry_event_id = make_event_id(user_id, f"{accion}-retry")
        retry = await enviar_drive(user_id, context, accion, event_id=retry_event_id)

        logger.info("retry accion=%s user=%s resp=%s", accion, user_id, retry)

        if retry.get("ok"):
            return retry

        if success_if_retry_error and retry.get("error") == success_if_retry_error:
            return {
                "ok": True,
                "recovered": True,
                "warning": success_if_retry_error
            }

        # ✅ Último recurso:
        # Si volvió a dar timeout, no bloqueamos el flujo.
        # En tu caso Apps Script suele registrar aunque el bot reciba timeout.
        if retry.get("error") == "timeout":
            return {
                "ok": True,
                "recovered": True,
                "warning": "timeout_assumed_success"
            }

        return retry

    return resp


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)

    await update.message.reply_text("⏳ Validando usuario...")

    user = await consultar_usuario(context, user_id, force=True)

    if not user.get("ok"):
        await update.message.reply_text(f"❌ Usuario no registrado\n🆔 {user_id}")
        return

    await cargar_referencias(context, user_id, force=True)
    reset_user_state(context, user_id, user.get("nombre"))

    await update.message.reply_text(
        f"👋 Hola {user.get('nombre')}",
        reply_markup=menu_principal()
    )


async def manejar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    texto = update.message.text.strip()
    user_id = get_user_id(update)

    if not context.user_data.get("user_id"):
        await update.message.reply_text("⏳ Validando usuario...")

        user = await consultar_usuario(context, user_id, force=True)

        if not user.get("ok"):
            await update.message.reply_text("❌ Usuario no válido")
            return

        context.user_data["user_id"] = user_id
        context.user_data["nombre"] = user.get("nombre")

    estado = context.user_data.get("estado", ESTADOS["MENU"])

    # =========================
    # INICIAR TURNO
    # =========================
    if texto == "🟢 Iniciar turno":
        if should_ignore_duplicate(context, "iniciar_turno"):
            return

        context.user_data["inicio"] = now_iso()
        context.user_data["estado"] = ESTADOS["REFERENCIA"]

        await update.message.reply_text("⏳ Cargando referencias...")

        ok = await cargar_referencias(context, user_id, force=False)

        if not ok:
            context.user_data["estado"] = ESTADOS["MENU"]
            await update.message.reply_text("❌ No pude cargar referencias", reply_markup=menu_principal())
            return

        await update.message.reply_text(
            "📌 Selecciona referencia",
            reply_markup=menu_referencias()
        )
        return

    # =========================
    # REFERENCIA INICIAL
    # =========================
    if estado == ESTADOS["REFERENCIA"]:
        ref = parse_referencia(texto)

        context.user_data["referencia"] = ref
        context.user_data["cantidad"] = ""
        context.user_data["cantidad_cerrada"] = ""
        context.user_data["cantidad_nueva"] = ""
        context.user_data["estado"] = ESTADOS["TRABAJANDO"]

        await update.message.reply_text("⏳ Registrando inicio de turno...")

        resp = await enviar_drive_with_recovery(
            user_id,
            context,
            "inicio",
            success_if_retry_error="ya_existe_sesion_activa"
        )

        if not resp.get("ok"):
            context.user_data["estado"] = ESTADOS["MENU"]
            await update.message.reply_text(
                f"❌ No pude iniciar el turno\nDetalle: {resp.get('error', 'sin detalle')}",
                reply_markup=menu_principal()
            )
            return

        await update.message.reply_text(
            f"🧵 Produciendo:\n{ref}",
            reply_markup=menu_trabajo()
        )
        return

    # =========================
    # PAUSA
    # =========================
    if texto == "⏸ Pausa":
        if should_ignore_duplicate(context, "pausa"):
            return

        context.user_data["pausa_inicio"] = now_iso()

        await update.message.reply_text("⏳ Registrando pausa...")

        resp = await enviar_drive_with_recovery(
            user_id,
            context,
            "pausa",
            success_if_retry_error="no_open_session"
        )

        if not resp.get("ok"):
            await update.message.reply_text(
                f"❌ No pude registrar la pausa\nDetalle: {resp.get('error', 'sin detalle')}",
                reply_markup=menu_trabajo()
            )
            return

        context.user_data["estado"] = ESTADOS["PAUSA"]

        await update.message.reply_text(
            "⏸ Pausa iniciada",
            reply_markup=menu_pausa()
        )
        return

    # =========================
    # REANUDAR
    # =========================
    if texto == "▶️ Reanudar":
        if should_ignore_duplicate(context, "reanudar"):
            return

        context.user_data["inicio"] = now_iso()

        await update.message.reply_text("⏳ Reanudando producción...")

        resp = await enviar_drive_with_recovery(
            user_id,
            context,
            "reanudar",
            success_if_retry_error="ya_existe_sesion_activa"
        )

        if not resp.get("ok"):
            await update.message.reply_text(
                f"❌ No pude reanudar\nDetalle: {resp.get('error', 'sin detalle')}",
                reply_markup=menu_pausa()
            )
            return

        context.user_data["estado"] = ESTADOS["TRABAJANDO"]

        await update.message.reply_text(
            "▶️ Continuando",
            reply_markup=menu_trabajo()
        )
        return

    # =========================
    # CAMBIO REFERENCIA
    # =========================
    if texto == "🔁 Cambio referencia":
        if should_ignore_duplicate(context, "cambio_menu"):
            return

        context.user_data["estado"] = ESTADOS["CAMBIO_CANTIDAD_CERRADA"]

        await update.message.reply_text("🔒 CANTIDAD CERRADA:")
        return

    if estado == ESTADOS["CAMBIO_CANTIDAD_CERRADA"]:
        if not is_positive_int(texto):
            await update.message.reply_text("❌ Ingresa una cantidad válida en números")
            return

        context.user_data["cantidad_cerrada"] = texto
        context.user_data["cantidad"] = texto
        context.user_data["estado"] = ESTADOS["CAMBIO_REF"]

        await update.message.reply_text("⏳ Cargando referencias...")

        ok = await cargar_referencias(context, user_id, force=False)

        if not ok:
            await update.message.reply_text("❌ No pude cargar referencias", reply_markup=menu_trabajo())
            return

        await update.message.reply_text(
            "📌 Nueva referencia:",
            reply_markup=menu_referencias()
        )
        return

    if estado == ESTADOS["CAMBIO_REF"]:
        ref = parse_referencia(texto)

        context.user_data["nueva_ref"] = ref
        context.user_data["estado"] = ESTADOS["CAMBIO_CANTIDAD_NUEVA"]

        await update.message.reply_text("🔢 CANTIDAD NUEVA:")
        return

    if estado == ESTADOS["CAMBIO_CANTIDAD_NUEVA"]:
        if not is_positive_int(texto):
            await update.message.reply_text("❌ Ingresa una cantidad válida en números")
            return

        cantidad_cerrada = context.user_data.get("cantidad_cerrada", "")
        nueva_ref = context.user_data.get("nueva_ref", "")
        cantidad_nueva = texto

        context.user_data["cantidad"] = cantidad_cerrada
        context.user_data["cantidad_cerrada"] = cantidad_cerrada
        context.user_data["cantidad_nueva"] = ""

        await update.message.reply_text("⏳ Cerrando referencia anterior...")

        cierre = await enviar_drive_with_recovery(
            user_id,
            context,
            "cambio",
            success_if_retry_error="no_open_session"
        )

        if not cierre.get("ok"):
            await update.message.reply_text(
                f"❌ No pude cerrar la referencia actual\nDetalle: {cierre.get('error', 'sin detalle')}",
                reply_markup=menu_trabajo()
            )
            return

        context.user_data["referencia"] = nueva_ref
        context.user_data["cantidad"] = cantidad_nueva
        context.user_data["cantidad_cerrada"] = ""
        context.user_data["cantidad_nueva"] = cantidad_nueva
        context.user_data["inicio"] = now_iso()

        await update.message.reply_text("⏳ Iniciando nueva referencia...")

        apertura = await enviar_drive_with_recovery(
            user_id,
            context,
            "inicio",
            success_if_retry_error="ya_existe_sesion_activa"
        )

        if not apertura.get("ok"):
            await update.message.reply_text(
                f"❌ No pude iniciar la nueva referencia\nDetalle: {apertura.get('error', 'sin detalle')}",
                reply_markup=menu_trabajo()
            )
            return

        context.user_data["estado"] = ESTADOS["TRABAJANDO"]

        await update.message.reply_text(
            f"🧵 Nueva producción:\n{context.user_data['referencia']}\n🔢 Cantidad nueva: {context.user_data['cantidad']}",
            reply_markup=menu_trabajo()
        )
        return

    # =========================
    # FINALIZAR
    # =========================
    if texto == "🔴 Finalizar jornada":
        if should_ignore_duplicate(context, "finalizar_menu"):
            return

        context.user_data["zonas"] = []
        context.user_data["estado"] = ESTADOS["ZONAS"]

        await update.message.reply_text(
            "📍 Selecciona zonas (puedes elegir varias y luego finalizar):",
            reply_markup=menu_zonas()
        )
        return

    if estado == ESTADOS["ZONAS"]:
        if texto == "✅ Terminar selección":
            context.user_data["fin"] = now_iso()
            context.user_data["zona"] = ", ".join(context.user_data.get("zonas", []))

            await update.message.reply_text("⏳ Finalizando jornada...")

            resp = await enviar_drive_with_recovery(
                user_id,
                context,
                "finalizar",
                success_if_retry_error="no_open_session"
            )

            if not resp.get("ok"):
                await update.message.reply_text(
                    f"❌ No pude finalizar la jornada\nDetalle: {resp.get('error', 'sin detalle')}",
                    reply_markup=menu_zonas()
                )
                return

            nombre = context.user_data.get("nombre")
            reset_user_state(context, user_id, nombre)

            await update.message.reply_text(
                "✅ Jornada finalizada\n🤝 Gracias por tu trabajo\n\nListo para iniciar nuevamente 🚀",
                reply_markup=menu_principal()
            )
            return

        zonas = context.user_data.setdefault("zonas", [])

        if texto in ZONAS and texto not in zonas:
            zonas.append(texto)

        await update.message.reply_text(
            f"✔️ Agregado: {texto}\nSelecciona más o finaliza"
        )
        return


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar))

    logger.info("🚀 BOT ONLINE")
    app.run_polling(drop_pending_updates=True, poll_interval=0.8)


if __name__ == "__main__":
    main()