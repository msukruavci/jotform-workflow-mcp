# MCP Akisi Calisma Notlari

Bu dosya mentor raporu degil; senin sistemi adim adim anlayip anlatabilmen
icin hazirlanmis calisma notu. Ana rapor:

```text
docs/mcp-flow-deep-dive.md
```

## 1. Buyuk Resim

Bu projede ChatGPT, Jotform API'ye direkt gitmiyor. ChatGPT sadece MCP tool
cagiriyor. MCP server da bu istegi aliyor, dogru JSON formatina ceviriyor,
Jotform public API'ye gonderiyor, sonucu geri okuyup dogruluyor ve ChatGPT'ye
structured result olarak donduruyor.

Anlatirken su cumleyi kullanabilirsin:

```text
We designed the MCP server as a safe translation layer between ChatGPT and
Jotform Workflow APIs. ChatGPT sends structured MCP tool arguments; the server
validates, normalizes, writes through Jotform's public API, logs everything,
and stores revisions before mutating operations.
```

Turkce anlamiyla:

```text
ChatGPT'nin niyetini dogrudan API'ye basmak yerine araya kontrollu bir MCP
katmani koyduk. Bu katman hem tool schema'larini expose ediyor hem de Jotform
builder'in bekledigi JSON formatini uretip dogruluyor.
```

## 2. Runtime Zinciri

Aktif zincir su:

```text
ChatGPT
-> OpenAI Secure Tunnel
-> tunnel-client
-> run_server.sh
-> ./.venv/bin/python -m mcp_server.server
-> AuditedMCPServer
-> MCP tools
-> JotformClient
-> api.jotform.com
```

Burada kritik nokta:

```text
api.py aktif degil.
```

Neden?

Tunnel profili `run_server.sh` calistiriyor. `run_server.sh` de
`python -m mcp_server.server` calistiriyor. Bu stdio MCP server demek.
`api.py` ise eski HTTP/SSE yaklasimina ait. Eger tunnel HTTP endpoint'e
baglansaydi `api.py` anlamli olurdu, ama su anki kurulumda yok.

Mentore kisa anlatim:

```text
Current plugin setup uses the stdio MCP path, not the HTTP/SSE api.py path.
This avoids an extra server layer, port management, and CORS issues.
```

## 3. Server Boot Mantigi

`mcp_server/server.py` cok kucuk tutuldu. Bunun sebebi mimariyi temiz ayirmak.

Yaptiklari:

```text
1. .env yukler
2. AuditedMCPServer olusturur
3. JotformClient olusturur
4. Tool modullerini register eder
5. mcp.run() ile stdio MCP server'i baslatir
```

Tool modulleri:

```text
discovery -> schema ve step type kesfi
reading   -> workflow/form okuma ve inspection
building  -> workflow/form/step/link yazma
risky     -> delete/publish/restore gibi onay isteyen islemler
```

Bu ayrim onemli cunku:

```text
Read-only operations ile mutating operations ayriliyor.
Riskli operasyonlarda iki asamali confirm pattern var.
Schema kesfi ayri oldugu icin ChatGPT once neyi kullanabilecegini ogreniyor.
```

## 4. ChatGPT'den MCP'ye Giden Veri

ChatGPT MCP tool cagirirken JSON arguments gonderir.

Ornek:

```json
{
  "name": "add_step",
  "arguments": {
    "workflow_id": "262251776639972",
    "step_type": "workflow_send_email",
    "config": {
      "name": "Application received",
      "to": ["{q3_email1}"],
      "subject": "We received your application",
      "content": "Hi {q2_fullname0}, thanks for applying."
    },
    "after_step_id": "1",
    "intent": "Send application receipt",
    "reason": "Notify applicant after submission"
  }
}
```

Buradaki mantik:

```text
workflow_id  -> hangi workflow uzerinde calisacagiz
step_type    -> hangi tip node eklenecek
config       -> node'un ayarlari
after_step_id-> direkt hangi node'un altina baglanacak
intent       -> log/debug icin kisa amac
reason       -> neden bu islemi yaptigimiz
```

`intent` ve `reason` user query'nin tamamini loglamak yerine privacy-conscious
kisa ozet tutmak icin var.

## 5. MCP'den ChatGPT'ye Donen Veri

Tool'lar duz text dondurmuyor. Pydantic result schema donduruyor.

Ornek:

```json
{
  "step_id": "2",
  "type": "workflow_send_email",
  "linked_from": "1",
  "warnings": [
    "wrapped plain text email content as HTML"
  ],
  "error": null,
  "hint": null
}
```

Kural:

```text
error null ise islem basarili.
error dolu ise tool exception firlatmak yerine problemi data olarak dondurdu.
hint varsa ChatGPT bir sonraki adimi oradan anlamali.
```

Bu UX icin onemli:

```text
Tool patladi ve ChatGPT hicbir sey anlamadi durumu yerine,
"Bu adimi ekleyemiyorum cunku approver eksik. Kullaniciya tek bir kisa soru sor."
seklinde ilerliyoruz.
```

## 6. Tool Schema Mantigi

Her MCP tool'un iki tarafi var:

```text
Input schema  = ChatGPT'nin MCP'ye gonderecegi arguments
Output schema = MCP'nin ChatGPT'ye dondurecegi structured result
```

Ornek input:

```text
connect_steps(workflow_id, from_step_id, to_step_id, outcome, intent, reason)
```

Ornek output:

```text
ConnectStepsResult {
  link_id,
  from_step,
  to_step,
  outcome,
  error,
  hint
}
```

Mentore anlatirken:

```text
The schema is the contract. ChatGPT does not need to know raw Jotform payloads;
it only needs tool arguments. The server then maps those arguments to the
builder-compatible JSON.
```

## 7. Discovery Tools

### list_step_types

Amaci:

```text
Hangi workflow step tipleri destekleniyor, UI'da adlari ne, schema var mi?
```

Donus:

```text
StepTypeList -> step_types[]
```

Icinde:

```text
step_type
category
description
ui_name
canonical_type
subtype
schema_available
```

Buradaki `canonical_type` ve `subtype` cok onemli. Cunku bazi UI elementleri
ayri API type degil.

Ornek:

```text
workflow_send_pdf
  canonical_type = workflow_send_email
  subtype        = workflow_send_pdf
```

Yani ChatGPT "PDF step" der, MCP `workflow_send_pdf` gorur, ama Jotform'a
giderken API type aslinda `workflow_send_email`, subType `workflow_send_pdf`.

### get_step_schema

Amaci:

```text
Bir step tipini eklemek/guncellemek icin hangi config field'lari gonderilebilir?
```

Donus:

```text
StepSchema {
  step_type,
  canonical_type,
  subtype,
  description,
  ui_name,
  fields[],
  error,
  hint,
  available_types[]
}
```

`fields[]` icinde:

```text
name
type
description
fixed_value
allowed_values
item_fields
```

Bu yuzden ChatGPT once `get_step_schema`, sonra `add_step` yapmali.

## 8. Reading Tools

### list_workflows

Workflow listesini getirir. Kullanici "su workflow'u ac" dediginde ID bulmak
icin kullanilir.

Output:

```text
WorkflowList {
  workflows: [
    workflow_id,
    workflow_url,
    title,
    status,
    updated_at,
    run_count
  ],
  error
}
```

### get_workflow

Bu en kritik read tool'lardan biri.

Ne getirir?

```text
workflow metadata
steps[]
connections[]
health
diagnostics
```

`steps[]` node listesidir. `connections[]` link listesidir.

`health` sunlari soyler:

```text
unreachable_steps
dead_end_steps
unknown_types
dangling_links
unconnected_branches
invalid_branch_links
unlabelled_branching_steps
```

Anlatirken:

```text
get_workflow is not only a read endpoint wrapper. It also interprets the
workflow graph and reports structural health.
```

### get_step_details

`get_workflow` step summary dondurur. Full config icin `get_step_details`
kullanilir.

Ornek full config:

```text
email subject
email content
to/cc/bcc
assignee
outcomes
pause config
condition terms
```

### inspect_workflow_gaps

Bu tool UX guardrail icin var.

Kontrol ettikleri:

```text
bos email recipient/content
bos assignee/approver
bos task description
unconnected branch outcome
dangling link
invalid condition field id
unlabelled branch link
```

Bu tool sonucunda ChatGPT "workflow ready" demeden once eksikleri gorur.

### list_forms / get_form_fields

Workflow trigger form'a bagli oldugu icin form bilgisi cok onemli.

`get_form_fields` su durumlarda gerekir:

```text
Email kime gidecek? -> gercek email field bulunur
Condition hangi field'a bakacak? -> label degil field_id kullanilir
Email content field injection -> {q2_fullname0} gibi gercek token gerekir
```

## 9. Building Tools

### create_form_with_ai

Sadece form olusturur. Workflow baglamaz.

Input:

```text
prompt
form_type
language
intent
reason
```

Jotform endpoint:

```text
POST /workflow/copilot/createWorkflowForm
```

Output:

```text
form_id
form_url
title
summary
questions
error
```

### create_workflow

Mevcut bir form ile workflow olusturur.

Kural:

```text
trigger_form_id yoksa normalde calismamali.
allow_without_trigger=true sadece kullanici explicit draft isterse.
```

Neden?

Workflow'un anlamli baslamasi icin trigger form gerekiyor.

### create_workflow_with_ai_form

Yeni form + workflow beraber olusturur.

Akis:

```text
1. AI ile form olustur
2. form_id al
3. workflow olustur
4. formu trigger olarak bagla
5. start point'i read-back ile dogrula
```

Bu bizim formsuz workflow problemini cozen ana ozellik.

### add_step

Bir node ekler.

Ic akis:

```text
1. schema_registry ile step_type resolve edilir
2. config validate edilir
3. eksik kritik bilgi var mi bakilir
4. condition field id gercek mi kontrol edilir
5. assignee/email field normalize edilir
6. duplicate step var mi bakilir
7. position hesaplanir
8. revision snapshot alinir
9. updateTree ile element create edilir
10. after_step_id varsa link create edilir
```

Neden bos step olusturmuyoruz?

```text
Bos task/approval/email/condition UI'da kirik gorunebilir. Bunun yerine tool
error + hint dondurup ChatGPT'nin kullaniciya kisa soru sormasini sagliyoruz.
```

### connect_steps

Iki node'u baglar.

Branching olmayan step:

```text
outcome bos olmali
```

Branching step:

```text
outcome zorunlu
```

Branching step tipleri:

```text
workflow_binary_decision
workflow_conditional_branch
workflow_approval
workflow_assign_task
```

Kritik detay:

```text
Link tek basina branch anlamini tasimaz. Branch anlamini source element'in
outcomes[].linkID alani tasir.
```

Bu yuzden `connect_steps`:

```text
1. link create eder
2. link label update eder
3. source outcome linkID update eder
```

### disconnect_steps

Sadece link siler, node silmez.

Eger link branching step'ten cikiyorsa:

```text
once ilgili outcome.linkID temizlenir
sonra link silinir
```

Bunu yapmazsak outcome hala eski linkID'ye bakar ve UI/MCP tarafinda dangling
mapping olusur.

### update_step

Mevcut node config'ini gunceller. Linkleri degistirmez.

Onemli:

```text
outcomes update edilirken mevcut linkID'ler korunur.
```

Yoksa branch isimlerini guncellerken baglantilari yanlislikla koparabiliriz.

## 10. Risky Tools

Risky tools iki asamali calisir:

```text
confirm=false -> preview only
confirm=true  -> gercek write
```

### delete_step

`confirm=false`:

```text
hangi connection'lar etkilenecek onu dondurur
hicbir sey silmez
```

`confirm=true`:

```text
1. incident links bulunur
2. source branch outcomes temizlenir
3. revision snapshot alinir
4. element + links updateTree ile silinir
5. read-back verification yapilir
```

Output'ta `verified=true` varsa:

```text
step silindi
linkler silindi
outcome linkID referanslari temizlendi
```

### publish_workflow

`confirm=false` health warning dondurur.

Kontrol eder:

```text
unreachable steps
dead ends
dangling links
unconnected branch outcomes
unlabelled branching links
invalid branch mappings
```

`confirm=true` publish eder.

### restore_workflow_revision

Undo mekanizmasi.

`confirm=false`:

```text
hangi revision'a donulecek preview edilir
```

`confirm=true`:

```text
once current state backup olarak kaydedilir
sonra eski snapshot restore edilir
```

### delete_workflow

Workflow tamamen siler. Extra guvenlik:

```text
confirm_title exact match ister
```

Boylece yanlis workflow ID secilse bile title uyusmazsa silme olmaz.

## 11. Jotform updateTree Mantigi

Jotform Workflow builder'da asil write endpoint:

```text
PUT /workflow/{workflow_id}/updateTree
```

Payload iki array tasir:

```text
elements[]
links[]
```

Element create:

```json
{
  "action": "create",
  "elementID": 2,
  "data": {
    "element_id": 2,
    "type": "workflow_send_email",
    "name": "Application received"
  }
}
```

Link create:

```json
{
  "action": "create",
  "linkID": 7,
  "data": {
    "link_id": 7,
    "fromElement": "3",
    "toElement": "4",
    "type": "default-link",
    "points": [{"a": "1"}],
    "fromPortName": "DYNAMIC_BOTTOM_1_Out",
    "toPortName": "DYNAMIC_TOP_1_In"
  }
}
```

Branch connect icin ek olarak source element update:

```json
{
  "action": "update",
  "elementID": 3,
  "data": {
    "element_id": 3,
    "outcomes": [
      {
        "outcomeID": 1,
        "text": "Proceed to Interview",
        "linkID": 7
      }
    ]
  }
}
```

## 12. Email ve Field Injection

Email recipient dynamic ise string olarak sadece `{q3_email1}` gondermek
yetmez. Builder-compatible object gonderiyoruz:

```json
{
  "id": "uuid",
  "value": "{q3_email1}",
  "text": "Email Address",
  "isValid": true,
  "isQuestion": true,
  "style": {
    "backgroundColor": "#007862",
    "--pillColor": "#007862"
  },
  "isBright": false,
  "formTitle": "Candidate Application Form"
}
```

Fixed email:

```json
{
  "id": "uuid",
  "value": "hr@example.com",
  "text": "hr@example.com",
  "isValid": true,
  "isQuestion": false
}
```

Content token normalize:

```text
{2} -> {q2_fullname0}
{5} -> {q5_textbox3}
```

Bu sayede UI'da field injection dogru gorunuyor.

## 13. Audit Log

Audit log dosyalari:

```text
mcp_server/logs/sessions/{started_at}_{session_id}.jsonl
```

Event tipleri:

```text
mcp.list_tools.started
mcp.list_tools.completed
mcp.tool_call.started
mcp.tool_call.completed
jotform.request.started
jotform.request.completed
```

Neyi logluyoruz?

```text
tool name
tool arguments
duration
result
Jotform method/url/status/body
```

Neyi redakt ediyoruz?

```text
apiKey
authorization
cookie
password
secret
token
bearer/basic/access_token gibi value'lar
```

Query loglama:

```text
ChatGPT user query direkt server'a gelmiyor. Server sadece tool arguments'i
gorebilir. Bu yuzden user query yerine intent/reason alanlariyla kisa,
privacy-conscious ozet tutuyoruz.
```

## 14. Revision Log

Revision log workflow bazli:

```text
mcp_server/revisions/{workflow_id}.jsonl
```

Ne zaman snapshot alinir?

```text
add_step oncesi
update_step oncesi
connect_steps oncesi
disconnect_steps oncesi
delete_step oncesi
publish_workflow oncesi
restore_workflow_revision oncesi
```

Neyi saklar?

```text
workflow metadata
elements
links
full element details
reason
session_id
timestamp
revision_id
```

Bu sayede:

```text
Bir onceki hale donmek icin restore_workflow_revision kullanilabilir.
```

## 15. En Kritik Invariants

Bunlari ezberlemen iyi olur:

```text
1. Workflow formsuz baslamamali.
2. Bos task/email/approval/condition eklenmemeli.
3. Condition field label degil real field_id kullanmali.
4. Dynamic email recipient real form email field olmali.
5. Branch link icin links[] yetmez, outcomes[].linkID de set edilmeli.
6. Delete step linkleri ve outcome refs'i de temizlemeli.
7. Risky tools preview-first olmali.
8. Mutating tools revision almadan yazmamali.
9. Her tool result error/hint ile ChatGPT'ye yol gostermeli.
10. api.py aktif tunnel yolunda degil.
```

## 16. Mentor'a Anlatim Sirasi

Sunumda bu sirayi takip edebilirsin:

```text
1. First, I clarified the active runtime path: ChatGPT uses the secure tunnel
   to run server.py over stdio, not api.py.

2. Then I split the MCP server into discovery, reading, building, and risky
   tool layers.

3. I added schema-driven guardrails so ChatGPT can discover step schemas
   before writing config.

4. I fixed workflow creation so it starts with a trigger form, either existing
   or AI-generated.

5. I normalized builder-specific JSON for email recipients, assignees, field
   tokens, outcomes, and pause configs.

6. I added revision snapshots before every mutating operation.

7. I added structured audit logging for MCP tool calls and Jotform API calls.

8. Finally, I added inspection and publish-preview checks so incomplete
   workflows are detected before telling the user they are ready.
```

Kapanis cumlesi:

```text
The main value is that ChatGPT no longer blindly writes workflow JSON. It now
operates through schemas, guardrails, read-back verification, logs, and
revision snapshots.
```
