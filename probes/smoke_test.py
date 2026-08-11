"""
MCP smoke test — tool katmanının üstünden, modelin gördüğü yoldan.

Neden client'ı değil de tool'ları çağırıyor: client'ın çalışması
tool'un çalıştığını göstermez. Modele giden şey tool'un döndürdüğü
şekil, ve asıl kırılganlık orada (alan adı değişmiş, .get() None
dönüyor, hata yutuluyor). Bu yüzden test mcp.call_tool() üzerinden.

Çalıştır:
    python -m probes.smoke_test
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from mcp_server.server import mcp  # noqa: E402

RESULTS: list[dict] = []


def _unwrap(raw):
    """
    mcp 2.0'da call_tool bir CallToolResult döndürüyor (1.x'teki tuple
    değil). structuredContent varsa onu al; list dönen tool'lar {"result": [...]}
    içine sarılıyor.
    """
    if isinstance(raw, tuple):  # eski SDK
        content, structured = raw
        raw = None
    else:
        structured = getattr(raw, "structuredContent", None)
        content = getattr(raw, "content", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    if content:
        # NOT: mcp 2.0, list[dict] döndüren tool'lara structuredContent
        # üretmiyor — her liste elemanını ayrı bir text bloğu olarak
        # gönderiyor. Yani modelin gördüğü şey 33 ayrı JSON parçası.
        parsed = []
        for c in content:
            text = getattr(c, "text", None)
            if not text:
                continue
            try:
                parsed.append(json.loads(text))
            except (ValueError, TypeError):
                parsed.append(text)
        if len(parsed) == 1:
            return parsed[0]
        return parsed
    return raw


async def check(label: str, tool: str, args: dict, expect) -> object:
    """expect(result) -> str | None. None dönerse PASS, str dönerse FAIL sebebi."""
    t0 = time.perf_counter()
    try:
        result = _unwrap(await mcp.call_tool(tool, args))
        reason = expect(result)
    except Exception as e:  # noqa: BLE001
        result, reason = None, f"exception: {type(e).__name__}: {e}"
    ms = int((time.perf_counter() - t0) * 1000)
    RESULTS.append({"label": label, "tool": tool, "ok": reason is None,
                    "reason": reason, "ms": ms})
    status = "PASS" if reason is None else "FAIL"
    print(f"[{status}] {label:<42} {ms:>5}ms" + (f"  <- {reason}" if reason else ""))
    return result


def has_error(r) -> str | None:
    """Tool'lar hatayı exception değil veri olarak döndürüyor — onu yakala."""
    if isinstance(r, dict) and r.get("error"):
        return str(r["error"])[:160]
    return None


async def main() -> int:
    if not os.environ.get("JOTFORM_API_KEY"):
        print("JOTFORM_API_KEY yok — .env dosyasını doldur.")
        return 2

    print("=" * 74)
    print("KATMAN 1 — keşif (ağ yok, tamamen yerel; burası kırılırsa paket bozuk)")
    print("=" * 74)

    def types_ok(r):
        if has_error(r):
            return has_error(r)
        types = (r or {}).get("step_types", [])
        if len(types) < 30:
            return f"beklenen ~34 tip, gelen {len(types)}"
        names = {t["step_type"] for t in types}
        if "workflow_placeholder" in names:
            return "internal tipler sızmış (placeholder listede görünüyor)"
        return None

    await check("list_step_types() tüm tipler", "list_step_types", {}, types_ok)
    await check("list_step_types('basic')", "list_step_types", {"category": "basic"},
                lambda r: None if (r or {}).get("step_types") else "boş döndü")

    def email_schema_ok(r):
        if has_error(r):
            return has_error(r)
        fields = {f["name"]: f for f in r.get("fields", [])}
        if "to" not in fields:
            return "'to' alanı yok"
        if fields["to"].get("type") == "any":
            return "'to' hâlâ type=any (allOf düzleştirme çalışmıyor)"
        if not fields["to"].get("description"):
            return "'to' açıklamasız — model ne göndereceğini bilemez"
        if any(f["name"] in ("x", "y") for f in r.get("fields", [])):
            return "koordinatlar sızmış"
        return None

    await check("get_step_schema('workflow_send_email')", "get_step_schema",
                {"step_type": "workflow_send_email"}, email_schema_ok)

    def bad_type_ok(r):
        if not isinstance(r, dict) or "error" not in r:
            return "geçersiz tipe hata dönmedi"
        if "hint" not in r or "available_types" not in r:
            return "hata mesajı modele ne yapacağını söylemiyor (hint/available_types yok)"
        return None

    await check("get_step_schema(geçersiz) -> öğretici hata", "get_step_schema",
                {"step_type": "workflow_send_emailz"}, bad_type_ok)

    print()
    print("=" * 74)
    print("KATMAN 2 — okuma (gerçek hesap, gerçek ağ)")
    print("=" * 74)

    def wf_list_ok(r):
        if has_error(r):
            return has_error(r)
        items = (r or {}).get("workflows", [])
        if not items:
            return "hesapta hiç workflow yok — önce UI'dan bir tane oluştur"
        if items[0].get("workflow_id") is None:
            return f"workflow_id None — alan adı değişmiş olabilir: {list(items[0])}"
        return None

    workflows = await check("list_workflows()", "list_workflows", {}, wf_list_ok)

    forms = await check("list_forms()", "list_forms", {},
                        lambda r: has_error(r) or (None if (r or {}).get("forms") else "hiç form yok"))

    form_items = (forms or {}).get("forms", []) if isinstance(forms, dict) else []
    if form_items and form_items[0].get("form_id"):
        fid = form_items[0]["form_id"]

        def fields_ok(r):
            if has_error(r):
                return has_error(r)
            fields = (r or {}).get("fields", [])
            if not fields:
                return "form alanı dönmedi"
            if fields[0].get("field_id") is None:
                return "field_id None"
            return None

        await check(f"get_form_fields({fid})", "get_form_fields", {"form_id": fid},
                    fields_ok)
    else:
        print("[SKIP] get_form_fields — kullanılabilir form yok")

    wf_items = (workflows or {}).get("workflows", []) if isinstance(workflows, dict) else []
    if not (wf_items and wf_items[0].get("workflow_id")):
        print("\n[SKIP] get_workflow / get_step_details — workflow yok")
        return summarize()

    wid = wf_items[0]["workflow_id"]

    def wf_ok(r):
        if has_error(r):
            return has_error(r)
        if not r.get("steps"):
            return "steps boş — /combined elements döndürmüyor olabilir"
        if r["steps"][0].get("step_id") is None:
            return "step_id None — /combined 'element_id' yerine başka bir ad kullanıyor"
        types = {s.get("type") for s in r["steps"]}
        if "workflow_start_point" not in types:
            return f"start point yok, gelen tipler: {types}"
        if any(s.get("label") in (None, "") for s in r["steps"]):
            return "bazı adımların label'ı boş — varsayılan üretilmemiş"
        return None

    wf = await check(f"get_workflow({wid})", "get_workflow", {"workflow_id": wid}, wf_ok)

    # connections ayrı raporlanıyor: boş olması hata değil (tek adımlı akış
    # olabilir) ama bilmek lazım — tree_builder buna bağlı.
    if isinstance(wf, dict) and wf.get("steps"):
        health = wf.get("health") or {}
        print(f"       -> {len(wf['steps'])} adım, {len(wf.get('connections', []))} bağlantı")
        print(f"       -> ulaşılamayan: {health.get('unreachable_steps')} | "
              f"çıkmaz: {health.get('dead_end_steps')} | "
              f"bilinmeyen tip: {health.get('unknown_types')}")
        labelled = sum(1 for c in wf.get("connections", []) if c.get("outcome"))
        print(f"       -> dal etiketi olan bağlantı: {labelled}/{len(wf.get('connections', []))}")
        if wf.get("diagnostics"):
            print(f"       !! {wf['diagnostics']}")
            print("       -> `python -m probes.inspect_links` çalıştır")

        sid = wf["steps"][0].get("step_id")
        if sid is not None:
            def step_ok(r):
                if has_error(r):
                    return has_error(r)
                if not isinstance(r, dict) or not r:
                    return "boş config döndü"
                return None

            detail = await check(f"get_step_details({wid}, {sid})", "get_step_details",
                                 {"workflow_id": wid, "step_id": str(sid)}, step_ok)
            if isinstance(detail, dict) and detail and not has_error(detail):
                print(f"       -> anahtarlar: {sorted(detail)[:12]}")

    return summarize()


def summarize() -> int:
    passed = sum(r["ok"] for r in RESULTS)
    total = len(RESULTS)
    print()
    print("=" * 74)
    print(f"SONUÇ: {passed}/{total} geçti")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  FAIL {r['label']}: {r['reason']}")
    print("=" * 74)

    with open("probes/smoke_test_result.json", "w") as f:
        json.dump({"passed": passed, "total": total, "results": RESULTS}, f, indent=2)
    print("Ayrıntı: probes/smoke_test_result.json")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
