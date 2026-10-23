"""Real-model probe of every model purpose (KTD1, R27): one small call per purpose through the provider the app
itself builds for it (its model, effort and cache parameters), each with the purpose's real instructions and
strict schema and a synthetic Hebrew input. The agent purpose runs the real tool list: a step that may call a
tool, a synthetic tool output, and a final step without tools. Vision reads a small generated picture.

Prints per call only: purpose, status, model, effort, token counts (input, cached, cache write, output) and
latency. Never the key, never a prompt or a reply. Not part of pytest; it calls the real API and costs money.

    scripts/check-model-egress.sh   # first
    docker compose exec backend python scripts/probe_models.py

Exit status 0 when every call is ``ok``, else 1.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import MODEL_PURPOSES  # noqa: E402
from app.providers import llm  # noqa: E402
from app.providers.llm import CallStatus, Purpose, call_structured  # noqa: E402

# Synthetic content only: invented street, town and figures.
QUESTION = "מה שכר הדירה החודשי למ\"ר בנכס ברחוב הדמה 7 בעיירת הבדיה?"
PASSAGE = "שכר הדירה החודשי בנכס ברחוב הדמה 7 בעיירת הבדיה הוא 62 ₪ למ\"ר, לא כולל מע\"מ."
TOOL_OUTPUT = json.dumps({"results": [{"sid": "S1", "title": "שומה לדוגמה — הדמה 7", "location": "עמ' 3",
                                       "text": PASSAGE}]}, ensure_ascii=False)


def _picture() -> bytes:
    """A small synthetic table (digits and Latin labels: the default bitmap font has no Hebrew)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (420, 120), "white")
    draw = ImageDraw.Draw(img)
    for i, row in enumerate((("Unit", "Area m2", "Rent ILS"), ("A", "84", "5,208"), ("B", "112", "6,944"))):
        for j, cell in enumerate(row):
            draw.text((20 + j * 130, 20 + i * 30), cell, fill="black")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def report(purpose: str, provider, result) -> bool:
    status = llm.CallStatus(result.status).value
    print(f"{purpose:8} status={status:17} model={getattr(result, 'model', None) or provider.model}"
          f" effort={getattr(provider, 'reasoning_effort', '') or '-'}"
          f" input={result.input_tokens} cached={result.cached_input_tokens} cache_write={result.cache_write_tokens}"
          f" output={result.output_tokens} latency_ms={result.latency_ms}"
          + (f" detail={result.detail}" if result.detail and status != "ok" else ""))
    return result.status == CallStatus.OK


def probe_agent() -> bool:
    from app.chat import tools as T
    from app.chat.engine import FINAL_SCHEMA, POLICY

    agent = llm.get_provider(Purpose.AGENT)
    key = llm.office_cache_key("model-probe")
    items: list = [{"role": "user", "content": QUESTION}]
    first = agent.agent_step(POLICY, items, T.TOOLS, FINAL_SCHEMA, cache_key=key, max_output_tokens=3000)
    ok = report("agent", agent, first)
    print(f"{'':8} tool_calls={len(first.calls)}")
    if not ok:
        return False
    items.extend(first.output)
    for c in first.calls:
        items.append({"type": "function_call_output", "call_id": c.call_id, "output": TOOL_OUTPUT})
    if not first.calls:
        return True  # answered without a tool: the final schema held
    final = agent.agent_step(POLICY, items, [], FINAL_SCHEMA, cache_key=key, max_output_tokens=3000)
    return report("agent", agent, final) and final.final is not None


def probe_structured(purpose: Purpose) -> bool:
    from app.chat import resolve, verify
    from app.measurements import extract

    p = llm.get_provider(purpose)
    if purpose == Purpose.RESOLVE:
        args = (resolve.POLICY, "ההודעה החדשה:\n" + QUESTION, resolve.ResolvedRequest, 1500)
    elif purpose == Purpose.VERIFY:
        rendered = (f'<evidence id="S1">{PASSAGE}</evidence>\n'
                    '<unit index="1">שכר הדירה החודשי הוא 62 ₪ למ"ר, לא כולל מע"מ [S1].</unit>')
        args = (verify.JUDGE_POLICY, rendered, verify.JudgeOutput, 2000)
    elif purpose == Purpose.MEASURE:
        passage = extract.Passage("P1", "text", PASSAGE, 0, None, "שכירות", False)
        args = (extract.INSTRUCTIONS, extract.batch_input([passage]), extract.ExtractionOutput, 4000)
    else:  # summary
        from app.chat.api import SUMMARY_POLICY, _Summary

        args = (SUMMARY_POLICY, f"סיכום קודם:\nאין\n\nהודעות נוספות:\nמשתמש: {QUESTION}\nעוזר: {PASSAGE}",
                _Summary, 600)
    instructions, text, schema, limit = args
    return report(purpose.value, p, call_structured(p, purpose, instructions, text, schema, max_output_tokens=limit))


def probe_vision() -> bool:
    from app.extraction.vision import VISION_INSTRUCTIONS, VisionSchema

    p = llm.get_provider(Purpose.VISION)
    result = p.structured_image(Purpose.VISION, VISION_INSTRUCTIONS, "תמלל את התמונה לפי ההוראות.", _picture(),
                                VisionSchema, max_output_tokens=3000)
    return report("vision", p, result)


def main() -> int:
    if not llm.selected_provider_configured():
        print("no key configured for the selected provider: nothing sent")
        return 1
    results = {}
    for name in MODEL_PURPOSES:
        purpose = Purpose(name)
        try:
            if purpose == Purpose.AGENT:
                results[name] = probe_agent()
            elif purpose == Purpose.VISION:
                results[name] = probe_vision()
            else:
                results[name] = probe_structured(purpose)
        except Exception as exc:  # noqa: BLE001 - a broken probe is a result, printed without its message
            print(f"{name:8} status=probe_error type={type(exc).__name__}")
            results[name] = False
    failed = [n for n, ok in results.items() if not ok]
    print("all purposes ok" if not failed else f"failed: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
