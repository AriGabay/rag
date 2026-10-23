"""Provider mode and connection test (KTD5, R8). The single source of truth for which provider answers.

Mode, derived per office from the cloud setting, the server key and the last connection test:

- ``cloud``: enabled, key present, and the last test passed or has not run (``untested``).
- ``error``: enabled and the last test failed, or enabled with no key (``missing_key``). Turns run limited
  with a limitation naming the failure, never the demo mock.
- ``demo``: disabled and ``DEMO_MODE`` (the clearly labeled mock answers).
- ``limited``: disabled and not demo (answers are assembled from the sources themselves); also when the
  office's acknowledgement names another provider than the selected one (``reacknowledge_required``): the
  consent an admin gave for one provider never sends office content to another, demo mode or not, until an
  admin acknowledges the selected provider again.

The connection test sends a fixed synthetic Hebrew prompt (no office content) through the structured call
and checks the parsed echo; with no key it returns ``missing_key`` without any network call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from app.config import MODEL_PURPOSES, get_settings
from app.providers import llm
from app.providers.llm import CallStatus, LLMProvider, Purpose, StructuredResult

logger = logging.getLogger(__name__)

MISSING_KEY = "missing_key"
REACKNOWLEDGE = "reacknowledge_required"  # consent was given for another provider than the selected one
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic (Claude)"}
RETENTION_NOTES = {
    "openai": "הבקשות נשלחות ל-OpenAI ללא שמירת תגובות (store=false), אך OpenAI עשויה לשמור יומני ניטור"
              " שימוש לרעה עד 30 יום, אלא אם לארגון הוגדרה שמירת אפס נתונים (ZDR).",
    "anthropic": "Anthropic עשויה לשמור את הבקשות לפרק זמן מוגבל לפי מדיניות שמירת הנתונים שלה,"
                 " אלא אם לארגון הוגדרה שמירת אפס נתונים (ZDR).",
}
# Hebrew reason per failure status, used in the limitation of error-mode answers.
FAILURE_REASONS = {
    MISSING_KEY: "לא נמצא בשרת מפתח עבור ספק המודל",
    CallStatus.AUTH: "ספק המודל דחה את המפתח (שגיאת הרשאה)",
    CallStatus.MODEL_UNAVAILABLE: "המודל שנבחר אינו זמין עבור המפתח",
    CallStatus.TIMEOUT: "ספק המודל לא הגיב בזמן",
    CallStatus.RATE_LIMITED: "חריגה ממגבלת קצב הבקשות של ספק המודל",
    CallStatus.QUOTA: "מכסת השימוש אצל ספק המודל נוצלה",
    CallStatus.REFUSAL: "ספק המודל סירב לבקשת הבדיקה",
    CallStatus.INCOMPLETE: "תשובת המודל נקטעה לפני סיומה",
    CallStatus.INVALID: "תשובת המודל לא תאמה את המבנה הנדרש",
    CallStatus.ERROR: "תקלה בפנייה לספק המודל",
}

TEST_ECHO = "בדיקת חיבור: שלום עולם 2026"
TEST_INSTRUCTIONS = "זוהי בדיקת חיבור טכנית. החזר בשדה echo את טקסט הקלט בדיוק כפי שהוא, ללא שינוי וללא תוספות."


class Mode(StrEnum):
    CLOUD = "cloud"
    ERROR = "error"
    DEMO = "demo"
    LIMITED = "limited"


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    echo: str


@dataclass(frozen=True)
class LastTest:
    provider: str | None
    model: str | None
    ok: bool
    status: str
    tested_at: datetime | None


@dataclass(frozen=True)
class ProviderState:
    mode: Mode
    enabled: bool
    provider: str  # selected provider kind: openai | anthropic
    model: str
    key_present: bool
    status: str | None  # the failure behind ``error`` mode, else None
    untested: bool  # cloud mode without a passing test for the selected provider and model
    last_test: LastTest | None

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAMES.get(self.provider, self.provider)

    def limitation(self) -> str | None:
        """The Hebrew limitation a turn shows in this mode (None in cloud and demo mode)."""
        if self.status == REACKNOWLEDGE:
            return (f"שליחת קטעים לספק מודל ענן אושרה במשרד עבור ספק אחר, ולא עבור {self.provider_name}"
                    " שנבחר כעת; נדרש אישור מחדש של מנהל המשרד. התשובה מורכבת מקטעי המקור עצמם.")
        if self.mode == Mode.ERROR:
            reason = FAILURE_REASONS.get(self.status or "", FAILURE_REASONS[CallStatus.ERROR])
            return (f"שימוש במודל ענן מופעל במשרד, אך {reason} ({self.provider_name}); "
                    "התשובה מורכבת מקטעי המקור עצמם.")
        if self.mode == Mode.LIMITED:
            return "שליחת קטעים לספק מודל ענן כבויה במשרד; התשובה מורכבת מקטעי המקור עצמם."
        return None


def selected_provider_and_model() -> tuple[str, str]:
    """The provider kind and the conversation's model that settings select (never the key). The connection test
    runs on this model."""
    return get_settings().llm_provider, llm.purpose_model(Purpose.AGENT)[0]


def purpose_models() -> list[dict]:
    """The model and reasoning effort of every model purpose (KTD1), for the admin status screen."""
    return [{"purpose": p, "model": model, "effort": effort or None}
            for p in MODEL_PURPOSES for model, effort in [llm.purpose_model(p)]]


def consent_matches(consent_provider: str | None, provider: str) -> bool:
    """Whether the office's acknowledgement covers the selected provider. ``put_office_settings`` records the
    selected provider with every acknowledgement; an office acknowledged before the provider changed (e.g.
    ``anthropic`` while OpenAI is selected) must acknowledge again. A row enabled outside that route, with no
    provider recorded, predates provider tracking and is read as consent for the selected provider."""
    return consent_provider is None or consent_provider == provider


def derive_mode(*, enabled: bool, key_present: bool, last_test: LastTest | None, demo_mode: bool,
                provider: str, model: str, consent_provider: str | None = None) -> tuple[Mode, str | None, bool]:
    """(mode, status, untested). A test of another provider or model, or a ``missing_key`` result from before
    a key was added, does not describe the current configuration and counts as not run. Consent given for
    another provider is ``limited`` with status ``reacknowledge_required`` (never the demo mock: the office did
    opt in to cloud use, and its admin must see why it is not used)."""
    if not enabled:
        return (Mode.DEMO if demo_mode else Mode.LIMITED), None, False
    if not consent_matches(consent_provider, provider):
        return Mode.LIMITED, REACKNOWLEDGE, False
    if not key_present:
        return Mode.ERROR, MISSING_KEY, False
    current = last_test if (last_test is not None and last_test.provider == provider
                            and last_test.model == model and last_test.status != MISSING_KEY) else None
    if current is not None and not current.ok:
        return Mode.ERROR, current.status, False
    return Mode.CLOUD, None, current is None


def office_provider_state(conn: Connection, *, key_present: bool | None = None) -> ProviderState:
    """The office's provider state. ``key_present`` may be passed by a caller that resolves it itself."""
    row = conn.execute(text(
        "SELECT cloud_llm_enabled, cloud_provider, provider_test_provider, provider_test_model, provider_test_ok,"
        " provider_test_status, provider_tested_at FROM office_settings")).one_or_none()
    enabled = bool(row and row.cloud_llm_enabled)
    last = (LastTest(row.provider_test_provider, row.provider_test_model, bool(row.provider_test_ok),
                     row.provider_test_status, row.provider_tested_at)
            if row is not None and row.provider_test_status else None)
    if key_present is None:
        key_present = llm.selected_provider_configured()
    provider, model = selected_provider_and_model()
    mode, status, untested = derive_mode(enabled=enabled, key_present=key_present, last_test=last,
                                         demo_mode=get_settings().demo_mode, provider=provider, model=model,
                                         consent_provider=row.cloud_provider if row is not None else None)
    return ProviderState(mode, enabled, provider, model, key_present, status, untested, last)


@dataclass
class ConnectionTestResult:
    provider: str
    model: str
    status: str
    client: LLMProvider | None = None  # the provider called; None when no call was made
    result: StructuredResult | None = None

    @property
    def ok(self) -> bool:
        return self.status == CallStatus.OK


def run_connection_test() -> ConnectionTestResult:
    """One structured call with the synthetic prompt through the selected provider. Holds no DB connection."""
    provider_kind, model = selected_provider_and_model()
    if not llm.selected_provider_configured():
        return ConnectionTestResult(provider_kind, model, MISSING_KEY)
    client = llm.get_selected_provider()
    try:
        result = client.structured(Purpose.TEST, TEST_INSTRUCTIONS, TEST_ECHO, EchoOutput)
    except Exception as exc:  # noqa: BLE001 - an unexpected client failure is a status, never a crash
        logger.warning("provider %s connection test failed: %s", provider_kind, type(exc).__name__)
        return ConnectionTestResult(provider_kind, model, CallStatus.ERROR.value, client)
    status = result.status.value
    if result.ok and " ".join(str(result.parsed.echo).split()) != TEST_ECHO:
        status = CallStatus.INVALID.value
    return ConnectionTestResult(provider_kind, model, status, client, result)


def record_test(conn: Connection, outcome: ConnectionTestResult) -> None:
    conn.execute(
        text("UPDATE office_settings SET provider_test_provider = :p, provider_test_model = :m,"
             " provider_test_ok = :ok, provider_test_status = :s, provider_tested_at = now()"),
        {"p": outcome.provider, "m": outcome.model, "ok": outcome.ok, "s": outcome.status},
    )
