# Jotform Workflow MCP Server — Türkçe Açıklamalı Tam README

`mcp_server/` —
yani asıl ürün — neredeyse satır satır işlendi. Geri kalan her şey
(`probes/`, `tests/`, `docs/`) "ne kontrol ediyor ve ne kanıtladı"
seviyesinde anlatıldı, çünkü yazarı dışında kimsenin ihtiyaç duyduğu
detay seviyesi bu.

---

## İçindekiler

1. [Proje, tek paragrafta](#proje-tek-paragrafta)
2. [Repo düzeni](#repo-düzeni)
3. [Mimari](#mimari)
4. [`mcp_server/` — satır satır](#mcp_server--satır-satır)
   - [`jotform_client.py`](#jotform_clientpy)
   - [`schema_registry.py`](#schema_registrypy)
   - [`graph.py`](#graphpy)
   - [`tree_builder.py`](#tree_builderpy)
   - [`models.py`](#modelspy)
   - [`tools/discovery.py`](#toolsdiscoverypy)
   - [`tools/reading.py`](#toolsreadingpy)
   - [`tools/building.py`](#toolsbuildingpy)
   - [`tools/risky.py`](#toolsriskypy)
   - [`server.py`](#serverpy)
5. [`tests/` — gerçekte ne kanıtlanmış](#tests--gerçekte-ne-kanıtlanmış)
6. [`probes/` — her biri tek satır](#probes--her-biri-tek-satır)
7. [`docs/` — hikâyenin yaşadığı yer](#docs--hikâyenin-yaşadığı-yer)
8. [Tasarımı şekillendiren bulgular](#tasarımı-şekillendiren-bulgular)
9. [Server'ı çalıştırmak](#serverı-çalıştırmak)
10. [Şu anki durum](#şu-anki-durum)

---

## Proje, tek paragrafta

Altı haftalık bir staj projesi: Jotform Workflows'u, bir AI asistanıyla
(Claude, ChatGPT) yapılan konuşmanın içinden, konuşmadan hiç çıkmadan
erişilebilir ve işlem yapılabilir hale getirmek — MCP (Model Context
Protocol) üzerinden. Kapsam, tool tasarımı ve arayüz brief tarafından
bilinçli olarak açık bırakıldı — bunu çözmek zaten görevin kendisiydi. Bu
repo, dört katmana (discovery, reading, building, risky) yayılmış 14
tool sunan tek bir MCP server — tamamen Jotform'un **public** (hem
dokümante edilmiş hem edilmemiş) API yüzeyine karşı kuruldu — Jotform'un
kendi builder UI'ını çalıştıran internal BFF'e hiç dokunulmadı, çünkü o
session ile korunuyor ve projenin ground rule'larına göre yasak.

## Repo düzeni

```
jotform-workflow-mcp/
├── mcp_server/       ürünün kendisi — modelin gerçekten konuştuğu her şey
│   ├── server.py           giriş noktası, tüm tool'ları register eder
│   ├── jotform_client.py   api.jotform.com etrafında HTTP wrapper
│   ├── schema_registry.py  Jotform'un ham JSON Schema'larını modelin
│   │                       okuyabileceği hale getirir
│   ├── graph.py             saf reachability/health analizi, ağ yok
│   ├── tree_builder.py      saf fonksiyonlar: niyet -> updateTree payload'ı
│   ├── models.py            her tool için Pydantic dönüş şekilleri
│   ├── schemas/
│   │   └── workflow_all_schemas.json   Jotform'un ham element şemaları
│   └── tools/
│       ├── discovery.py     list_step_types, get_step_schema
│       ├── reading.py       list_workflows, get_workflow, ...
│       ├── building.py      create_workflow, add_step, connect_steps, ...
│       └── risky.py         delete_step, publish_workflow, delete_workflow
├── tests/            unit testler — ağ yok, API key yok, <2 saniyede biter
│   ├── test_graph.py
│   └── test_tree_builder.py
├── probes/           gerçek Jotform API'sine karşı tek seferlik ve
│   │                 tekrarlanabilir script'ler — docs/'taki her iddia
│   │                 buraya kadar takip edilebilir
│   └── (~35 script — aşağıdaki özet tabloya bak)
├── docs/
│   ├── gap-report.md      neyin çalıştığı doğrulanmış, neyin
│   │                      doğrulanmadığı ve her satırın nasıl doğrulandığı
│   │                      — "bu ürün gerçekte ne yapabiliyor" sorusunun
│   │                      canlı kaynağı
│   └── decision-log.md    her önemsiz görünmeyen kararın, tarihli,
│                          alternatifiyle ve neden kaybettiğiyle birlikte
│                          kaydı
├── bruno/            manuel/interaktif API test koleksiyonu
├── run_server.sh     giriş noktası script'i — bkz. "Server'ı çalıştırmak"
└── requirements.txt
```

## Mimari

```
 Kullanıcı
   |
   v
 Claude / ChatGPT  <-- NE yapılacağına model karar verir; sistemde
   |                   karar veren tek şey bu
   | MCP protokolü (yerelde stdio, ya da uzak bir connector)
   v
 mcp_server/  <-- NASIL yapılacağına deterministik olarak karar verir.
   |              Kendi LLM'ine hiçbir şey sormaz — bu kutunun içinde
   |              hiç LLM yok.
   v
 api.jotform.com
```

Dört tool katmanı, bu sırayla kuruldu, her biri sadece kendinden
öncekine bağımlı:

1. **Discovery** — "neyi ekleyebilirim?" Yerel bir JSON dosyasını bir kez
   yüklemek dışında hiç ağ yok.
2. **Reading** — "bu workflow neye benziyor, ve sağlıklı mı?" Salt
   okunur, serbestçe çağrılması güvenli.
3. **Building** — "bu değişikliği yap." Yazıyor, ama hiçbir şey yıkıcı
   değil.
4. **Risky** — silme ve yayınlama. Buradaki her tool, bir şey yapmadan
   önce ikinci, açık bir onay çağrısı istiyor.

---

## `mcp_server/` — satır satır

### `jotform_client.py`

HTTP ile konuşan tek dosya. Bunun üstündeki her şey Python dict'leriyle
çalışıyor; bu projenin geri kalanı (bunun altındaki her şey) hiçbir yerde
doğrudan `requests` import etmiyor.

```python
BASE_URL = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TIMEOUT = 20
```
Base URL hardcoded bir sabit değil, environment'tan geliyor, çünkü
Jotform'un bölgesel base'leri var (EU/HIPAA) — ileride işe yarayabilir.
20 saniyelik timeout — keyfi ama cömert; bu projede şu ana kadar hiçbir
şey daha uzun sürmedi.

```python
class JotformAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jotform API error {status}: {body[:300]}")
        self.status = status
        self.body = body
```
Özel bir exception tipi — `requests`'in ham exception'ının doğrudan
yükselmesine izin vermek yerine. Dört katmandaki her tool
`except JotformAPIError as e:` diye dar bir yakalama yapıyor. Bu geniş
bir `except Exception` olsaydı, *bizim* kodumuzdaki gerçek bir bug (bir
yazım hatası, bir `None.get()`) sessizce yutulup modele "API başarısız
oldu" diye raporlanırdı ki bu, debug sırasında doğrudan yanıltıcı olurdu.

```python
class JotformClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("JOTFORM_API_KEY", "")
        if not self.api_key:
            raise ValueError("JOTFORM_API_KEY is not set")
```
Key yoksa, construction anında hemen ve yüksek sesle patlıyor — üç tool
çağrısı sonra ilk API çağrısında değil. `server.py`, import anında tek
bir `JotformClient` kuruyor, yani eksik bir key, belirsiz bir şekilde her
tool'un ayrı ayrı başarısız olması yerine, tüm server'ı boot'ta net bir
hatayla öldürüyor.

```python
def _request(self, method, path, *, params=None, json_body=None) -> dict:
    params = dict(params or {})
    params["apiKey"] = self.api_key
    resp = requests.request(method, f"{BASE_URL}{path}", params=params,
                             json=json_body, timeout=TIMEOUT)
    if resp.status_code not in (200, 201):
        raise JotformAPIError(resp.status_code, resp.text)
    return resp.json()
```
Sınıftaki her diğer metodun döküldüğü tek boğaz noktası. `apiKey` burada
bir kez ekleniyor, yani aşağıdaki hiçbir metodun onu eklemeyi hatırlaması
gerekmiyor. Baştaki alt çizgi (`_request`) bir konvansiyon, zorlama değil
— "internal, bu sınıfın dışındaki çağıranlar bunu doğrudan kullanmasın"
demek, ve sınıftaki her public metod tam olarak bunu yapıyor.

**Okuma metodları** (hepsi doğrulanmış çalışıyor, hepsi salt okunur,
serbestçe çağrılması güvenli):

- `list_forms(status=None)` → `GET /user/forms`
- `get_form_questions(form_id)` → `GET /form/{id}/questions?parseJSON=1`
- `list_workflows()` → `GET /user/workflows`, DELETED/PURGED/ARCHIVED'ı
  dışlayan bir filtreyle. **Hiçbir yerde dokümante edilmemiş** — bir HAR
  capture'ının `x-raw-uri` response header'ından bulundu; bu header,
  client'a görünen URL farklı olsa bile sunucu tarafındaki route adını
  ifşa etmişti.
- `get_workflow_combined(workflow_id)` → `GET /workflow/{id}/combined?fetchEssentialElementProps=1`
  — metadata + elements + links **tek** çağrıda. Docstring "üç ayrı
  çağrıya tercih edilir" diyor çünkü dedicated `/links` endpoint'inin
  döndürdüğü her linkin döndüğü bağımsız olarak doğrulandı (iki gerçek
  workflow'da kontrol edildi: 8/8 ve 7/7 — bkz. decision log, "Does
  /combined return every link").
- `get_workflow(workflow_id)` → `GET /workflow/{id}` — sadece metadata,
  elements/links yok. Sadece title/status'un yeterli olduğu yerlerde
  kullanılıyor (`delete_workflow`'un önizlemesi gibi), gereksiz yere
  tüm ağacı çekmemek için.
- `get_elements(workflow_id)` → `GET /workflow/{id}/elements` — özetlenmiş
  node listesi.
- `get_element(workflow_id, element_id)` → `GET /workflow/{id}/elements/{id}`
  — bir element'in **tam** config'i, `outcomes`, `conditionTerms` dahil,
  özet listenin atladığı her şeyle birlikte. Bir step'in tam olarak nasıl
  yapılandırıldığını bilmesi gereken her tool, `get_elements`'i değil
  bunu çağırıyor.
- `get_links(workflow_id)` → `GET /workflow/{id}/links`

**Yazma metodları:**

```python
def create_workflow(self, title: str, *, trigger_on_edit: str = "ENABLED") -> dict:
    return self._request("POST", "/workflow", json_body={
        "title": title, "triggerOnEdit": trigger_on_edit,
        "elements": [{"action": "update", "elementID": 1, "data": {
            "element_id": 1, "id": 1, "type": "workflow_start_point",
            "elementType": "workflow_start_point",
            "className": ["isStartPoint"],
            "position": {"x": 0, "y": 0}, "x": 0, "y": 0,
            "measured": {"width": 296, "height": 88},
        }}],
        "links": [],
    })
```
Buradaki start-point element'inin her alanı gerçek tarayıcı trafiğinden
birebir kopyalandı — Jotform'un UI'ı yeni bir workflow oluşturulduğunda
tam olarak bu şekli gönderiyor, garip görünen
`className: ["isStartPoint"]` işaretçisi dahil. Bu spesifik alanların
neden var olduğunu kimse bilmesine gerek duymadı; sunucunun beklediği
şey sadece bu, ve model bunları hiç görmüyor çünkü bu metodun tamamı
sadece bir `title` alıyor.

```python
def create_element(self, workflow_id: str, step_type: str) -> dict:
    """Creates a bare element. Only `type` is required; config comes after."""
    return self._request("POST", f"/workflow/{workflow_id}/elements",
                          json_body={"type": step_type})
```
**Bu metod, bu projedeki hiçbir tool tarafından kullanılmıyor.** Bu,
bağımsız olarak geçerli, ikinci bir yazma yolu (`POST /elements`,
önce-oluştur-sonra-yapılandır) — ayrı bir keşif hattı
(`probes/build_branching_workflow.py`) bunu gerçek tarayıcı trafiğinden
türeterek görünüşte başarıyla kullandı. Bu projedeki her tool bunun
yerine `update_tree`'yi `action:"create"` ile tek çağrıda kullanıyor —
bu projenin kendi harness'ında read-back doğrulamasıyla titizlikle
probe'lanmış yol. `create_element` silinmedi, saklanıyor — ileride bir
ihtiyaç (ör. bir element'i yapılandırmadan önce ID'sini bilmek istemek)
iki-çağrılı şekli tercih edilir kılarsa diye, dokümante edilmiş bir
alternatif olarak. Bkz. decision log, 2026-08-10, "Standardized
element/link writes on updateTree."

```python
def update_tree(self, workflow_id: str, *, elements: list | None = None,
                links: list | None = None) -> dict:
    """
    The master endpoint — add/update/delete elements and links in one
    call. This is what Jotform's own UI uses for every change, and
    it's the most reliable write path we found.
    """
    return self._request("PUT", f"/workflow/{workflow_id}/updateTree",
                          json_body={"elements": elements or [], "links": links or []})
```
**Bu koddaki en önemli tek metod.** `building.py` ve `risky.py`'deki
her tool nihayetinde bunu çağırıyor. `elements`/`links` içindeki her
girdide bir `action` (`"create"` / `"update"` / `"delete"`), bir id, ve
bir `data` dict var. `tree_builder.py`'nin tamamı, `data` dict'lerini
doğru kurmak için var.

```python
def set_trigger_form(self, workflow_id: str, form_id: str) -> dict:
    return self._request("POST", f"/workflow/{workflow_id}/setResource",
                          json_body={"resourceType": "FORM", "resourceID": form_id})
```
**Public API'de sessiz bir no-op olarak doğrulandı** (2026-08-10,
`probes/inspect_trigger_binding.py`) — `true` dönüyor, hiçbir yerde
hiçbir şeyi değiştirmiyor. `building.py`'deki `create_workflow` bunu
çağırıyor ama response'a hiç güvenmiyor; aşağıdaki bölüme bak.

```python
def publish_workflow(self, workflow_id: str) -> dict:
    return self._request("POST", f"/workflow/{workflow_id}/publish")
```
Kabul edildiği doğrulandı (`live: 1` içeren yapılandırılmış bir obje
dönüyor), ama etkisini doğrulamak için kontrol edilen metadata alanları
(`publishStatus`, `hasPublishedFlow`) güvenilmez çıktı — aşağıdaki
"Tasarımı şekillendiren bulgular"a bak.

```python
def delete_workflow(self, workflow_id: str) -> dict:
    """
    Confirmed working 2026-08-10 (probes/test_delete_workflow.py) —
    DELETE /workflow/{id}, verified by checking the workflow no longer
    appears in list_workflows afterward, not just the 200 response.
    """
    return self._request("DELETE", f"/workflow/{workflow_id}")
```
Bu fazda eklendi. `risky.py`'deki `delete_workflow` tool'u bunu
projedeki en katı onay deseniyle sarıyor (bkz. o bölüm).

---

### `schema_registry.py`

Jotform'un ham JSON Schema dosyasını (`schemas/workflow_all_schemas.json`,
36 step tipi, draft-07 JSON Schema) yükleyip, modelin gerçekten
okuyabileceği bir hale çeviriyor, gerçek katılık gerektiren yerler için
ham versiyonu da erişilebilir tutuyor.

```python
CATEGORIES = {
    "basic": [...8 tip...],
    "logic": [...7 tip...],
    "ai": [...13 tip...],
    "integration": [...6 tip...],
    "internal": [...3 tip...],
}
```
36 tip, düz listelemek için fazla — model her seferinde tümünü taramak
zorunda kalır. Kategoriler `list_step_types(category="basic")`'in önce
daraltmasını sağlıyor. `"internal"` (Jotform'un otomatik oluşturduğu,
bilerek eklenmesi hiç amaçlanmayan placeholder/generic tipler) varsayılan
listelemede hariç tutuluyor — `list_types()`'in `if cat != "internal"`
filtresiyle.

```python
DESCRIPTIONS = { ... 36 girdi ... }
```
Elle yazıldı, tip başına bir satır. **Jotform'un kendi şema `title`
alanının neden kullanılmadığı:** birkaç AI step tipi
(`workflow_ai_calculate`, `workflow_ai_categorize`,
`workflow_ai_summarize_text`, `workflow_ai_sentiment_analysis`) Jotform'un
kendi şema dosyasında **dördü de "Webhook Schema" başlıklı.** Bu alanı
okuyarak step tipi seçen bir model her seferinde yanlış seçerdi. Bu
dict, düzeltmesi — bakımı sıkıcı, ama alternatifi doğrudan yanıltıcı.

```python
UI_NAMES = {
    "workflow_binary_decision": "If/Else Condition",
    "workflow_payment_verification": "Payment Form",  # UNCONFIRMED
    ...
}
```
Jotform **builder UI**'ının her tipe verdiği ad, gerçek bir builder
ekran görüntüsüne karşı doğrulandı. Var olma sebebi: asistanla konuşan
kişi az önce o UI'ı kapatmış ya da ona bakıyor ve "bir approval adımı
ekle" diyor — API'nin değil, UI'ın kelime dağarcığı. İki girdi
`UNCONFIRMED` işaretli — en iyi tahminler, o elementi gerçekten yerleştirip
tipi geri okuyarak henüz doğrulanmadı.

```python
UNMAPPED_UI_ELEMENTS = ["Approve & Sign", "Team Approval", "Flow Report", "PDF"]
```
Ekran görüntüsünde görülen ama **hiçbir tip eşlemesi olmayan** builder-UI
elemanları. Belirsiz bir "şema eksik olabilir" değil, somut bir yapılacaklar
maddesi.

```python
BRANCHING_TYPES = {"workflow_binary_decision", "workflow_conditional_branch"}
```
Çıkan bir bağlantının anlamının, sadece var olmasından değil, adlandırılmış
bir outcome'dan (TRUE/FALSE, ya da özel bir dal ismi) geldiği step tipleri.
`reading.py` (çıkışta bağlantıları etiketlerken) ile
`tree_builder.py`/`building.py` (`connect_steps`'in ne zaman bir `outcome`
argümanı gerektirdiğine karar verirken) arasında **paylaşılıyor.** Burada,
tek yerde tanımlandı — özellikle okuma tarafıyla yazma tarafının hangi
tiplerin dallandığı konusunda birbirinden ayrışamaması için.

```python
def is_known_type(step_type: str) -> bool:
    return step_type in _load()
```
Canlı workflow'lar bu projenin şeması olmayan step tipleri içerebilir
(`workflow_payment_verification` gerçekten gözlemlenmiş bir örnek). Bir
step'in tipini raporlayan her çağıran bunu kontrol ediyor ve çökmek ya da
tipin yokmuş gibi davranmak yerine `known_type=False` set ediyor.

```python
def default_label(step_type: str | None) -> str:
    if not step_type:
        return "Unnamed step"
    return UI_NAMES.get(step_type) or step_type.replace("workflow_", "").replace("_", " ").capitalize()
```
Jotform, kullanıcının elle yeniden adlandırmadığı her step'te `name`'i
boş bırakıyor — üç etiketsiz email step'i olan bir workflow modele
okunmaz gelir ("bu emaili gönder" — hangisi?). UI adına, sonra son çare
olarak güzelleştirilmiş bir tip adına düşüyor.

```python
def _flatten_all_of(prop: dict) -> dict:
    if "allOf" not in prop:
        return prop
    merged = {k: v for k, v in prop.items() if k != "allOf"}
    for branch in prop["allOf"]:
        if isinstance(branch, dict):
            for k, v in branch.items():
                merged.setdefault(k, v)
    return merged
```
**Bu dosyadaki en yüksek etkili tek fonksiyon.** Jotform, düz bir
string/number olmayan her zengin alanın ($to$, $cc$, condition term'leri,
type'ı düz olmayan her şey) hem gerçek `$ref`'ini (tip bilgisi) hem de
`description`'ını bir JSON Schema `allOf` sarmalayıcısının içine
gizliyor. Bu fonksiyon var olmadan önce, bir email step'inin `to` alanı
(alıcı listesi — muhtemelen o step'teki en önemli tek alan) modele
`{"name": "to", "type": "any"}` diye, hiç açıklamasız görünüyordu.
36 şema genelinde ölçülen etki: **bu düzeltmeden önce 14 alan "any"e
düşüyordu, 90'ının açıklaması yoktu; sonra 2 ve 78** (kalanlar Jotform'un
kendi şemasında gerçekten dokümante edilmemiş — düzleştirmeyle
düzelmiyor).

```python
def _simplify_property(name: str, prop: dict, definitions: dict) -> dict:
    prop = _flatten_all_of(prop)
    if "$ref" in prop:
        ...
        items = resolved.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            entry["item_fields"] = {
                k: (v.get("type", "any") if isinstance(v, dict) else "any")
                for k, v in items["properties"].items()
                if k != "additionalProperties"
            }
        return entry
    ...
    if "const" in prop:
        entry["fixed_value"] = prop["const"]
    if "enum" in prop:
        entry["allowed_values"] = prop["enum"]
    if "anyOf" in prop:
        # enum/const bazen bir anyOf dalının içinde gizli
        ...
```
Array-of-object alanları için (ör. `to`, bir alıcı objesi array'i),
`item_fields` **bir** item'ın neye benzediğini gösteriyor — yoksa model
bir liste göndermesi gerektiğini bilir ama neyin listesi olduğunu
bilmez. `allowed_values`, bu dosyanın ürettiği en değerli tek bilgi
parçası: bir modelin geçerli bir enum değerini tahmin etmesiyle onu
tam olarak bilmesi arasındaki fark.

```python
def get_simplified_schema(step_type):
    ...
    fields = [f for f in fields if f["name"] not in ("x", "y")]
```
Tuval koordinatları modelin gördüğü şeyden çıkarılıyor — bir step'in
*ne yaptığına* karar vermek, görsel olarak nereye oturduğunu düşünmeyi
gerektirmemeli. (Konumlandırma ayrıca, `tree_builder.compute_position`'da
hesaplanıyor.)

```python
def list_types(category=None):
    ...
    return [{
        "step_type": name, "category": ...,
        "description": DESCRIPTIONS.get(name, ""),
        "ui_name": UI_NAMES.get(name),
        "schema_available": name in schemas,
    } for name in names]
```
Bu dosyanın kategorize ettiği ama şeması olmayan tipler (şu an
`workflow_payment_verification`) gizlenmek yerine `schema_available: false`
işaretiyle **yine de listeleniyor.** Gizlemek, modele gerçek, var olan bir
step tipinin var olmadığını söylerdi — "bu var ama senin için
yapılandıramıyorum" demekten daha kötü.

---

### `graph.py`

Bu projede standart kütüphane dışında **sıfır** bağımlılığı olan tek
dosya — HTTP yok, MCP yok, aldığı alan adları dışında Jotform'a özgü
hiçbir kelime dağarcığı yok. Bu, onu tüm sistemin kanıtlanabilir tek
parçası yapıyor, sadece makul değil — bu yüzden buradaki dosyanın satır
başına birim test sayısı en yüksek.

```python
TERMINAL_TYPES = {"workflow_end_point"}
```
Ulaşılmış ve hiç çıkışı olmayan bir step normalde bir "çıkmaz sokak"tır
(bir hata) — açık bir end-point hariç, orada bu doğru ve kasıtlıdır. Bu
tek satırlık istisna, sağlık kontrolünün düzgün şekilde sonlandırılmış
her dalı bozuk diye işaretlememesi için var.

```python
def analyse(steps: list[dict], connections: list[dict]) -> dict:
```
Düz dict alıp döndürüyor, Pydantic model ya da MCP'ye özgü bir şey değil
— bu sınır kasıtlı, böylece bu fonksiyon hiçbir server çalışmadan, elle
kurulmuş bir fixture ile bir test içinde çağrılabiliyor.

```python
dangling = []
for c in connections:
    src, dst = c.get("from_step"), c.get("to_step")
    ...
    if src_s is not None and src_s not in id_set:
        dangling.append(f"link {c.get('link_id')}: from missing step {src_s}")
    elif dst_s is not None and dst_s not in id_set:
        dangling.append(f"link {c.get('link_id')}: to missing step {dst_s}")
```
Gerekli olup olmadığı doğrulanmadan önce eklendi — bir step'in silinmesinin
onun bağlantılarını arkada bırakıp hiçliğe işaret ettirdiğini
`probes/test_delete_impact.py`'nin kanıtlamasından **önce** eklenen
savunmacı bir kontrol. Cevap ne olursa olsun bir güvenlik ağı olarak
yazıldı; tam olarak cevap çıktı, ve sonradan da aynı yanlış varsayımı
yapan gelecekteki herhangi bir yazma yolu için genel amaçlı bir kontrol
olarak faydalı kaldı.

```python
roots = [i for i in ids if types.get(i) == "workflow_start_point"]
if not roots:
    roots = ids[:1]
```
Ulaşılabilirlik start point'ten hesaplanıyor — eğer bir tane yoksa (ki
olmaması gerekir, ama hiçbir şey garanti etmiyor), çökmek yerine
listedeki ilk step'e düşüyor, ve bu fallback sessiz bir varsayım değil,
açık, görünür bir satır.

```python
seen: set[str] = set()
stack = list(roots)
while stack:
    node = stack.pop()
    if node in seen:
        continue
    seen.add(node)
    stack.extend(outgoing.get(node, []))
```
Düz iteratif DFS. Stack'in bir sonraki genişlemesine eklemeden önceki
`if node in seen: continue`, bir döngünün (A step'i B'ye bağlanır, B
tekrar A'ya bağlanır) sonsuz döngüye yol açamamasını sağlıyor — bu sadece
varsayılmıyor, test suite'indeki `test_cycle_does_not_hang` ile
kanıtlanıyor.

```python
unreachable = [i for i in ids if i not in seen]
dead_ends = [
    i for i in ids
    if i in seen and not outgoing.get(i) and types.get(i) not in TERMINAL_TYPES
]
```
Kasıtlı olarak birbirine karıştırılmamış iki farklı problem:
**unreachable** = hiç çalışma şansı bulamıyor. **Dead end** = çalışıyor,
sonra akış olması gerekmeyen bir yerde duruyor. Bir workflow ikisinden
birine sahip olabilir ötekine sahip olmadan, ve bir kullanıcının
hangisinin olduğunu bilmesi gerekiyor.

---

### `tree_builder.py`

**Kod tabanındaki en önemli dosya.** "Buraya bu tür bir step ekle" ya da
"bu iki step'i şu outcome ile bağla"yı, Jotform'un API'sinin kabul
edeceği tam `updateTree` payload'ına çeviriyor — ve bunu saf fonksiyonlar
olarak yapıyor, hiç ağ çağrısı yok, ki bu onu tamamen unit-test edilebilir
kılan şey (24 test, aşağıya bak).

```python
LINK_DEFAULTS = {
    "type": "default-link",
    "points": [{"a": "1"}],
    "fromPortName": "DYNAMIC_BOTTOM_1_Out",
    "toPortName": "DYNAMIC_TOP_1_In",
}
```
Bu projedeki en yük taşıyan tek sabit. Bu dört değerin her biri bağımsız
olarak ölçüldü, tahmin edilmedi:

- `points` — **boş olmamalı; içeriği tamamen önemsiz.** Boş bir liste
  (`[]`) API tarafından sanki alan hiç yokmuş gibi reddediliyor (PHP
  `empty()` semantiği — `[]` ile yok aynı muameleyi görüyor).
  `[{"a": "1"}]` boş olmadığı için kabul edilen ve olduğu gibi saklanan
  anlamsız çöp.
- `fromPortName` / `toPortName` — **var olması zorunlu, ama değeri hiç
  doğrulanmıyor.** Anlamsız bir şey göndermek (`"BANANA_Out"`) kabul
  ediliyor, ve sunucu bunu geri okumada **sessizce gerçek, doğru porta
  yeniden yazıyor.** Bu doğrudan kanıtlandı, çıkarım yapılmadı: çöp port
  isimleriyle oluşturulmuş bir link, hemen ardından geri okununca,
  Jotform'un kendi UI'ının kullanacağı kanonik port isimlerini gösterdi.
  Bu aynı zamanda portların branch anlamı **taşıyamayacağını** da
  kanıtlıyor — sunucu, kendisine ne verilirse özgürce üzerine yazdığı
  için, bir linkin hangi branch'i temsil ettiğini sadece linkten bilme
  yolu yok.
- `type` — zorunlu, **hiç doğrulanmıyor, ve asla düzeltilmiyor.**
  Buradaki bir yazım hatası (`"banana-link"` test edildi) sonsuza kadar
  kalıcı oluyor, **yazma anında hiç hata yok ve bir şey onu yorumlamaya
  çalışana kadar görünür bir belirti yok.** Bu asimetri — portlar
  kendini düzeltiyor, `type` düzeltmiyor — tam olarak neden `type`'ın
  burada hardcoded bir sabit olduğu ve hiçbir tool'un public
  parametrelerinde hiç görünmediği. Hiçbir model, hiçbir zaman, bu değeri
  etkileyemez.

```python
STEP_Y = 180
BRANCH_X = 340
```
Layout aralık sabitleri. `STEP_Y` (bir step ile ondan sonra yerleştirilen
şey arasındaki dikey boşluk) kullanılıyor; `BRANCH_X` tanımlı ama
**şu an hiçbir yerde kullanılmıyor** — hiç uygulanmamış, dalları yan yana
yerleştirme için daha önceki bir planın kalıntısı. Tuval layout'u açık
bir madde olarak kalıyor (bkz. gap-report.md madde 5) — aşağıdaki
`compute_position` çözülmüş değil, bir yer tutucu.

```python
class ValidationError(Exception):
    """A request that must not reach the API — caller error, not server error."""
```
`JotformAPIError`'dan ayrı. Bir `ValidationError`, "tool katmanı bunu
bir API çağrısı harcamadan önce yakaladı" demek — ör. bilinmeyen bir step
tipi, ya da var olmayan bir outcome. `building.py` ve `risky.py`'deki
her yakalama noktası ikisini ayırıyor, çünkü modele doğru cevap
farklılaşıyor: bir `ValidationError` genelde yerine ne denenmesi gerektiği
konusunda bir hint'le geliyor; bir `JotformAPIError` daha çok "harici bir
şey ters gitti"ye yakın.

```python
def next_id(existing: list[str | int | None]) -> int:
    nums = []
    for v in existing:
        try:
            nums.append(int(v))
        except (TypeError, ValueError):
            continue
    return (max(nums) + 1) if nums else 1
```
Element id'leri ve link id'leri, **çağıranın** atadığı küçük tam
sayılar — Jotform bir tane vermiyor. Bu her zaman **taze çekilmiş** bir
listeden hesaplanıyor (`building.py`'deki her çağrı noktası, bunu
çağırmadan hemen önce `client.get_elements` ya da `client.get_links`
çağırıyor), daha önceki bir konuşma turundan cache'lenmiş bir liste
değil, çünkü eski bir id listesi, zaten kullanımda olan bir id seçmek
demek — sessizce bir şeyin üzerine yazmanın gerçek bir yolu. `try/except`,
temiz bir tam sayı olmayan her şeyi atlıyor (Jotform'un bazen farklı
endpoint'ler arasında string/int id'leri karıştırmasına karşı savunmacı —
doğrudan gözlemlendi, okuma tarafındaki paralel sorun için
`test_ids_compare_as_strings_not_ints`'e bak).

```python
def compute_position(elements, after_step_id):
    positioned = [...]
    if after_step_id is None:
        base_y = max((y for _, y in positioned), default=0)
        return {"x": 0, "y": base_y + STEP_Y}
    anchor = next((e for e in elements if str(e.get("element_id")) == str(after_step_id)), None)
    anchor_pos = _position_of(anchor) if anchor else None
    if anchor_pos is None:
        base_y = max((y for _, y in positioned), default=0)
        return {"x": 0, "y": base_y + STEP_Y}
    ax, ay = anchor_pos
    return {"x": ax, "y": ay + STEP_Y}
```
Bunun ne olduğu konusunda dürüst: **gerçek** bir auto-layout **değil.**
Tuvaldeki başka hiçbir şeye karşı çakışma kontrolü yok. Bir anchor
verildiğinde, yeni step onun tam altına gidiyor. Verilmediğinde, en
alttaki mevcut step'in altına gidiyor. Yeni bir node'un asla ebeveyninin
tam üzerine gelmeyeceğini garanti ediyor; kalabalık bir tuvalde zaten var
olan başka bir şeyle görsel olarak çakışmayacağını **garanti etmiyor.**
Çözülmüş gibi sunulmuyor, açık bir eksik olarak işaretli.

```python
def validate_config(step_type: str, config: dict) -> tuple[dict, list[str]]:
    schema = schema_registry.get_simplified_schema(step_type)
    if schema is None:
        raise ValidationError(...)
    by_name = {f["name"]: f for f in schema["fields"]}
    clean, warnings = {}, []
    for key, value in (config or {}).items():
        if key in ("x", "y", "position", "type", "element_id", "id"):
            continue
        field = by_name.get(key)
        if field is None:
            warnings.append(f"unknown field '{key}' dropped")
            continue
        allowed = field.get("allowed_values")
        if allowed and value not in allowed:
            warnings.append(f"'{key}'={value!r} not in {allowed}; field dropped")
            continue
        clean[key] = value
    return clean, warnings
```
Bir modelin ayarlamaya çalıştığı her alan gerçek şemaya karşı kontrol
ediliyor. Bilinmeyen alanlar **bir uyarıyla düşürülüyor**, tamamen
reddedilmiyor ve sessizce de geçirilmiyor — birkaç konuşma turu önce
okuduğu bir şemadan çalışan bir model, var olmayan ya da o zamandan beri
değişmiş bir alan içerebilir; tek bir kötü alan yüzünden tüm isteği
başarısız kılmak gereksiz kırılgan olurdu, ama çöpü sessizce API'ye
göndermek daha kötü olurdu. Enum ihlalleri de aynı şekilde ele alınıyor.
Konumlandırma ve kimlik alanları (`x`, `y`, `position`, `type`,
`element_id`, `id`) her seferinde burada sessizce sıyrılıyor — bunlar
asla modelin ayarlayacağı şeyler değil (bkz.
`test_validate_config_strips_layout_and_identity_fields`).

```python
def _default_outcomes(step_type: str) -> list[dict] | None:
    raw = schema_registry.get_raw_schema(step_type) or {}
    default = ((raw.get("properties") or {}).get("outcomes") or {}).get("default")
    return copy.deepcopy(default) if isinstance(default, list) else None
```
**Gerçek, ölçülmüş bir bug'ı düzeltiyor.** Jotform'un JSON Schema'sı
`workflow_binary_decision` üzerinde `outcomes` için bir `default` değeri
listeliyor (standart TRUE/FALSE çifti) — ama o `default`, bir UI
**client**'ının bir formu neyle önceden doldurması gerektiğini
tanımlıyor; **sunucu bunu uygulamıyor.** `updateTree` üzerinden açık bir
`outcomes` array'i olmadan oluşturulan bir if/else element'i **hiç
olmadan** geri geliyor, kalıcı olarak — `connect_steps`'in o step için
artık bağlayacak hiçbir şeyi yok, asla. Erken bir entegrasyon testinde
her tek `connect_steps` çağrısının, taze oluşturulmuş bir decision
step'ine karşı `Available outcomes: []` ile başarısız olmasıyla keşfedildi.
`copy.deepcopy` burada önemli — onsuz, aynı step tipinden oluşturulan her
element aynı liste objesini paylaşır ve mutasyona uğratırdı.

```python
def build_element_create(step_type, element_id, config, position):
    data = {
        "element_id": element_id, "id": element_id,
        "type": step_type, "elementType": step_type,
        "position": position, "x": position["x"], "y": position["y"],
        "measured": DEFAULT_ELEMENT_SIZE,
        **config,
    }
    if step_type in schema_registry.BRANCHING_TYPES and "outcomes" not in data:
        defaults = _default_outcomes(step_type)
        if defaults:
            data["outcomes"] = defaults
    return {"action": "create", "elementID": element_id, "data": data}
```
Hem `element_id` hem `id` set ediliyor — Jotform'un ham payload'ları
farklı yerlerde iki key'i de kullanıyor, ve bu proje bunun neden böyle
olduğunu tam olarak hiç belirlemedi; ikisini de göndermek, çalıştığı
gözlemlenen şeyle örtüşüyor ve hiçbir bedeli yok. Varsayılan-outcome
enjeksiyonu, sadece çağıran (yani `validate_config`'in temizlenmiş
çıktısı) zaten `outcomes` sağlamamışsa çalışıyor — yani gelecekteki bir
çağıranın özel outcome'lar sağlaması saygı görüyor, üzerine yazılmıyor
(bkz. `test_caller_supplied_outcomes_are_not_overwritten`).

```python
def build_link_create(link_id, from_id, to_id):
    data = {"link_id": link_id, "fromElement": from_id, "toElement": to_id, **LINK_DEFAULTS}
    return {"action": "create", "linkID": link_id, "data": data}
```
Bu projedeki her link, iki ucun ne olduğuna bakılmaksızın, tam olarak
bu şekli alıyor. Link payload'larında step-tipi başına bir varyasyon yok
— bu projedeki daha hoş keşiflerden biri, çünkü orijinal korku, step tipi
başına farklı bir port modeline ihtiyaç duymaktı.

```python
def resolve_outcome(source_element: dict, outcome: str) -> dict:
    outcomes = source_element.get("outcomes") or []
    match = next((o for o in outcomes
                  if str(o.get("conditionValue", "")).lower() == outcome.lower()), None)
    if match is None:
        available = [o.get("conditionValue") for o in outcomes]
        raise ValidationError(f"'{outcome}' is not an outcome on this step. Available: {available}")
    if match.get("linkID"):
        raise ValidationError(
            f"Outcome '{outcome}' is already connected (to element "
            f"{_target_of_link(match.get('linkID'))}). ..."
        )
    return match
```
Büyük/küçük harf duyarsız eşleşme (`"true"`, `"TRUE"` ile eşleşiyor) —
bir modelin ikisini de yazması eşit derecede olası. **Herhangi bir API
çağrısından önce** yakalanan iki ayrı, adlandırılmış başarısızlık modu:
outcome bu step'te hiç yok, ya da var ama zaten bir şeye bağlı. Hiçbiri
sessizce devam edip yanlış şeyi bağlamıyor — bu fonksiyon sadece *geçerli,
bağlanmamış* bir outcome döndürüyor ya da hata veriyor.

```python
def build_outcome_update(source_element, outcome_id, link_id):
    outcomes = source_element.get("outcomes") or []
    updated = [
        {**o, "linkID": link_id} if o.get("outcomeID") == outcome_id else o
        for o in outcomes
    ]
    return build_element_update(source_element.get("element_id"), {"outcomes": updated})
```
**Tüm** `outcomes` array'ini geri gönderiyor, sadece eşleşen girdi
değişmiş olarak. `updateTree` alanları toptan değiştiriyor, birleştirerek
değil — sadece değişen bir outcome'u göndermek, o step'teki her diğer
outcome'u sessizce silerdi (ör. TRUE'yu bağlarken FALSE dalını silmek
gibi). Bu, dal bağlamanın uçtan uca gerçekte nasıl çalıştığını anlamak
için en önemli tek satır.

---

### `models.py`

Her tool'un dönüş tipi bir Pydantic modeli, çıplak bir `dict` ya da
`list[dict]` değil. Bu orijinal tasarım değildi — bir düzeltmeydi.
**MCP Inspector üzerinden bulundu:** `-> dict` diye işaretlenmiş bir
tool, MCP SDK'ya bir JSON Schema inşa etmesi için hiçbir şey vermiyor,
yani o tool'un en zengin verisi (örneğin `get_workflow`'un tüm
step/connection/health yapısı), client'ın yapısal olarak parse edebileceği
bir şey yerine yapılandırılmamış bir metin bloğu olarak geliyor. Aşağıdaki
her tek alan, bir tool'un tam olarak o bilgi parçasını döndürmesi
gerektiği için var — "ileride lazım olabilir" diye spekülatif hiçbir alan
yok.

Dört tool katmanıyla eşleşen dört gruba ayrılmış:

- **Discovery**: `StepTypeSummary`, `StepTypeList`, `SchemaField`, `StepSchema`
- **Reading**: `WorkflowSummary`/`WorkflowList`, `Step`, `Connection`
  (`outcome` ve `from_port` taşıyor — ikisinin de var olmasının ve çok
  farklı şeyler ifade etmesinin sebebi için aşağıdaki `reading.py`'ye
  bak), `WorkflowHealth` (`unreachable_steps`, `dead_end_steps`,
  `dangling_links`, `unconnected_branches`, `unknown_types` —
  `graph.py`'nin tespit edebileceği her farklı başarısızlık modu için
  bir alan), `WorkflowDetail`, `StepDetail`, `FormSummary`/`FormList`,
  `FormField`/`FormFieldList`
- **Building**: `CreateWorkflowResult`, `AddStepResult`
  (`warnings: list[str]` taşıyor — düşürülen/düzeltilen her config alanı
  burada ortaya çıkıyor, hiç sessizce değil), `ConnectStepsResult`,
  `UpdateStepResult`
- **Risky**: `DeleteStepResult`, `PublishWorkflowResult`,
  `DeleteWorkflowResult` — bu üçünün her biri `needs_confirmation: bool`'u
  başka alanların boş olmasından çıkarılan bir şey değil, birinci sınıf
  bir alan olarak taşıyor.

Neredeyse her model ayrıca opsiyonel bir `error: str | None = None`
taşıyor. Bu kasıtlı: **bu projedeki tool'lar hiçbir zaman exception'ı
modele kadar yükseltmiyor.** Bir exception modele eyleme geçirilebilir
hiçbir şey söylemez; okuyabileceği, anlayabileceği ve kişiye
açıklayabileceği bir `error` alanı söyler. `discovery.py`, `reading.py`,
`building.py`, `risky.py`'deki her `try/except JotformAPIError` bloğu
bu alanı doldurarak bitiyor, exception'ın yayılmasına izin vererek değil.

---

### `tools/discovery.py`

En küçük, en basit katman — iki tool, ikisi de kendine ait mantığı
neredeyse hiç olmayan, `schema_registry.py` üzerine ince wrapper'lar.

```python
@mcp.tool()
def list_step_types(category: str = "") -> StepTypeList:
    """..."""
    return StepTypeList(
        step_types=[StepTypeSummary(**t) for t in schema_registry.list_types(category or None)]
    )
```
`category: str | None = None` yerine `category: str = ""` — MCP SDK'nın
şema üretimi, farklı client implementasyonları arasında düz bir string
default'u, Optional'dan daha öngörülebilir şekilde işliyor; hemen
altındaki `category or None`, boş-string default'u
`schema_registry.list_types`'ın kendi imzası için tekrar `None`'a
çeviriyor. Aynı desen (tool sınırında `str = ""`, içeride dönüştürülüyor),
`building.py` ve `risky.py`'deki her opsiyonel string parametre için
tekrarlanıyor.

```python
@mcp.tool()
def get_step_schema(step_type: str) -> StepSchema:
    result = schema_registry.get_simplified_schema(step_type)
    if result is None:
        available = [t["step_type"] for t in schema_registry.list_types()]
        known = step_type in available
        if known:
            return StepSchema(
                step_type=step_type, ui_name=schema_registry.get_ui_name(step_type),
                error=f"No field schema on record for {step_type}.",
                hint="This is a real step type and may appear in existing workflows, "
                     "but this server cannot describe or configure its fields. "
                     "Tell the user it must be edited in Jotform.",
            )
        return StepSchema(
            error=f"Unknown step type: {step_type}",
            hint="Call list_step_types to see valid values.",
            available_types=available,
        )
    return StepSchema(...)
```
İki ayrı hata durumu, kasıtlı olarak tek bir şeye birleştirilmemiş: hiç
var olmayan bir step tipi (yazım hatası, halüsinasyon) ile gerçek olan —
şu anda kullanıcının gerçek workflow'unda oturuyor olabilir — ama bu
projenin dosyasında şeması olmayan bir step tipi. Tip gerçekken ve
sadece dokümante edilmemişken modele "unknown type" demek, modelin
kullanıcıya yanlış bir şey söylemesine ("o step tipi yok") sebep olurdu.
Her durumdaki `hint` alanı, modele sırada tam olarak ne yapması
gerektiğini söylüyor.

---

### `tools/reading.py`

Beş tool. Okuma katmanının açık farkla en karmaşık dosyası —
`get_workflow` tek başına dosyanın neredeyse yarısı — çünkü dal kimliği,
sağlık analizi ve tanılama (diagnostics) hepsi burada bir araya geliyor.

```python
def _outcome_map(elements: list[dict]) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    unconnected: list[str] = []
    for el in elements:
        if el.get("type") not in BRANCHING_TYPES:
            continue
        step_id = el.get("element_id")
        for outcome in el.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            label = outcome.get("conditionValue") or outcome.get("value")
            link_id = outcome.get("linkID")
            if link_id in (None, 0, "0", ""):
                unconnected.append(f"step {step_id} {label or outcome.get('outcomeID')}")
            elif label:
                mapping[str(link_id)] = str(label)
    return mapping, unconnected
```
**Bu fonksiyon, tüm projedeki en büyük yanlış dönüşün düzeltmesi.** Modül
docstring'i bunu açıkça kaydediyor:

> Bu modül başlangıçta bir link hakkındaki her şeyi iki ucu dışında
> atıyordu, ki bu bir if/else step'inin TRUE ve FALSE dalları arasındaki
> ayrımı kaybediyordu — bu anlam, tesisat (plumbing) değil.
>
> Bu etiketi aramak için bariz yer linkti. Orada değil: `labels` her
> linkte boş, ve `fromPortName` ("RIGHT_MIDDLE_Out") bir kenarın kutudan
> tuval üzerinde nereden çıktığını anlatıyor, ki bu dalla tesadüfen
> örtüşüyor ve makul, yanlış bir cevap olurdu. Etiket *karar veren
> element*'te yaşıyor, `outcomes[] = {conditionValue, linkID}` olarak.

Somut olarak: bu incelenirken elde bulunan tek gerçek if/else
workflow'unda, `fromPortName` gerçekten TRUE ile FALSE'la örtüşüyordu.
Bunu cevap olarak göndermek, o bir workflow'a karşı çalıştırılan her test
çalışmasını geçerdi ve genelde yanlış olurdu — korelasyon, o node'un
spesifik tuval layout'unun bir kazasıydı, alanın bir özelliği değil.
Düzeltme, eldeki tek örnekte daha sert pattern-match yapmak yerine,
Jotform'un `workflow_binary_decision` için kendi ham JSON Schema'sını
okumaktan geldi — o, `outcomes`'u açıkça tanımlıyor.
`link_id in (None, 0, "0", "")` kasıtlı olarak gevşek — Jotform'un,
bağlama göre, ayarlanmamış bir `linkID`'yi `None`, `0`, `"0"`, ya da
`""`'den herhangi biri olarak gönderdiği gözlemlendi, ve dördü de aynı
şeyi ifade ediyor: bu dal tanımlanmış ama hiçbir şeye bağlanmamış.

```python
@mcp.tool()
def get_workflow(workflow_id: str) -> WorkflowDetail:
    ...
    combined = client.get_workflow_combined(workflow_id)
    wf = combined.get("workflow", {}) or {}
    elements = [el for el in (combined.get("elements") or []) if isinstance(el, dict)]
    links = [ln for ln in (combined.get("links") or []) if isinstance(ln, dict)]
    outcome_by_link, unconnected_branches = _outcome_map(elements)
```
Tek bir API çağrısı (`/combined`), bu tool'un ihtiyaç duyduğu her şeyi
üretiyor — `isinstance(..., dict)` filtreleri var, çünkü Jotform'un ham
array'leri, pratikte, ara sıra dict-olmayan çöp girdiler içerdi.
Buradaki savunmacı filtreleme, tek bir bozuk girdinin tüm tool'u
çökertememesini sağlıyor.

```python
steps: list[Step] = []
unknown_types: list[str] = []
for el in elements:
    step_type = el.get("type")
    known = bool(step_type) and schema_registry.is_known_type(step_type)
    if step_type and not known and step_type not in unknown_types:
        unknown_types.append(step_type)
    steps.append(Step(
        step_id=str(el.get("element_id")) if el.get("element_id") is not None else None,
        type=step_type,
        label=el.get("name") or schema_registry.default_label(step_type),
        trigger_form_id=el.get("resourceID"),
        known_type=known,
    ))
```
Her step id'si açıkça `str`'ye çevriliyor — Jotform, farklı
endpoint'ler arasında, hatta aynı response içinde bağlama göre int ve
string id'leri karıştırıyor (doğrudan gözlemlendi ve özellikle
`tests/test_graph.py`'nin `test_ids_compare_as_strings_not_ints`'inde
test edildi), ve bu projedeki aşağı akış karşılaştırmaları hepsi string
id'leri tutarlı varsayıyor. `label`, `default_label`'dan önce
`el.get("name")`'den (kullanıcının kendi etiketi, ayarladıysa) geçiyor.

```python
connections = []
for ln in links:
    link_id = str(ln.get("link_id")) if ln.get("link_id") is not None else None
    connections.append(Connection(
        link_id=link_id,
        from_step=..., to_step=...,
        outcome=outcome_by_link.get(link_id or ""),
        from_port=ln.get("fromPortName"),
    ))
```
Hem `outcome` hem `from_port`, kasıtlı olarak, çok farklı iki şeyi
ifade eden iki ayrı alan olarak tutuluyor. `outcome`, bir modelin
okuyup üzerine düşünmesi gereken şey — gerçek dal kimliği. `from_port`
sadece bir linki geriye (`tree_builder.py`'de) yazmanın *bir şekilde*
bir port değeri gerektirmesi için tutuluyor, ve modele hiçbir zaman
anlamlıymış gibi gösterilmiyor — bunu anlamlı saymanın orijinal hata
olmasının sebebi için yukarıdaki düzeltme notuna bak.

```python
health_raw = graph.analyse(
    [s.model_dump() for s in steps],
    [c.model_dump() for c in connections],
)
```
Pydantic modelleri, özellikle `graph.py`'yi çağırmak için düz dict'lere
geri dönüştürülüyor — `graph.py`'nin, tek çağıranından bile, gerçekten
hiç Pydantic/MCP bağımlılığı olmadığını pekiştiriyor.

```python
diagnostics: dict = {}
branching_ids = {str(el.get("element_id")) for el in elements
                 if el.get("type") in BRANCHING_TYPES}
unlabelled = sorted(
    sid for sid in branching_ids
    if any(c.from_step == sid for c in connections)
    and not any(c.from_step == sid and c.outcome for c in connections)
)
if unlabelled:
    diagnostics["unlabelled_branching_steps"] = unlabelled
    diagnostics["note"] = (...)
```
Kendi kendini kontrol: eğer bir step dallandığı biliniyorsa, en az bir
çıkan bağlantısı varsa, ama bağlantılarının **hiçbiri** bir `outcome`
etiketi almadıysa, bu outcome-eşleme mantığının kullanılabilir hiçbir
şey bulamadığı anlamına gelir — belki Jotform'un veri şekli bu
yazıldığından beri değişti. Bunun yerine `outcome: null` olan
bağlantıları sessizce döndürüp "burada dal yok" gibi görünmesine izin
vermek yerine, bu tutarsızlığı açıkça ortaya çıkarıyor ve tekrar
çalıştırılacak spesifik probe script'ine (`inspect_outcomes.py`)
işaret ediyor.

```python
@mcp.tool()
def get_step_details(workflow_id: str, step_id: str) -> StepDetail:
    ...
    config = client.get_element(workflow_id, step_id)
```
Kasıtlı olarak `get_element`'i (tam, tek-element endpoint'i) çağırıyor,
zaten çekilmiş `get_workflow` verisinden türetilmiş bir şey değil —
çünkü `get_workflow`'un `steps` listesi tasarım gereği bir özet (tuval
gürültüsü sıyrılmış), ve bu tool tam olarak o özet yetmediğinde diye var.

`list_forms` ve `get_form_fields` karşılaştırılabilir bir karmaşıklığı
olmayan basit wrapper'lar — id/title/status/submission count'la formları
listeliyor; koşul alanı ya da email alıcı alanı seçmek için kullanılan,
id/label/type/required'la bir formun alanlarını listeliyor.

---

### `tools/building.py`

Dört tool. Her biri aynı şekli takip ediyor: mevcut state'i çek
(konuşmadaki daha önceki bir şeyden cache'lenmiş hiçbir şeye güvenme),
payload'ı hesaplaması için `tree_builder`'a ver, yaz, ne olduğunu
raporla.

```python
@mcp.tool()
def create_workflow(title: str, trigger_form_id: str = "") -> CreateWorkflowResult:
    ...
    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    ...
    if trigger_form_id:
        try:
            client.set_trigger_form(workflow_id, trigger_form_id)
            elements = client.get_elements(workflow_id)
            start = next((e for e in elements if e.get("type") == "workflow_start_point"), {})
            if str(start.get("resourceID")) != str(trigger_form_id):
                return CreateWorkflowResult(
                    workflow_id=str(workflow_id), title=title,
                    error=(
                        "Workflow created, but the trigger form could not be "
                        "bound — this is a known limitation of the public API, "
                        "not a failure you can retry. ..."
                    ),
                )
        except JotformAPIError as e:
            return CreateWorkflowResult(..., error=f"... trigger form failed: {e}")
    return CreateWorkflowResult(workflow_id=str(workflow_id), title=title,
                                trigger_form_id=trigger_form_id or None)
```
Bu, tüm projedeki en yeni mantık (2026-08-10), ve gerçek bir bug
yüzünden var: `set_trigger_form` `true` döndürüyor ve hiçbir şeyi
değiştirmiyor (workflow ve start point'indeki her alanı öncesi/sonrası
karşılaştırarak `probes/inspect_trigger_binding.py`'de doğrulandı — hiçbir
yerde hiçbir alan değişmedi). Bu tool'un **eski** versiyonu o `true`'ya
güveniyor ve başarıyı raporluyordu. Bu versiyon, start point element'ini
geri okuyor ve `resourceID`'nin gerçekten gönderilene eşit olup olmadığını
kontrol ediyor — bu, **bir okumayla doğrulanmış bir yazma**, bu
projedeki her diğer yazma tool'unun zaten takip ettiği aynı disiplin.
`create_workflow`, bu disiplinin gevşediği tek yerdi, ve bunun özellikle
probe'lanana kadar fark edilmemesinin sebebi bu.

```python
@mcp.tool()
def add_step(workflow_id, step_type, config, after_step_id=""):
    try:
        clean_config, warnings = tb.validate_config(step_type, config)
    except tb.ValidationError as e:
        return AddStepResult(error=str(e), hint="Call list_step_types to see valid values.")

    elements = client.get_elements(workflow_id)

    after_id = after_step_id or None
    if after_id is not None:
        links = client.get_links(workflow_id)
        existing_exit = next((l for l in links if str(l.get("fromElement")) == str(after_id)), None)
        if existing_exit is not None:
            return AddStepResult(
                error=f"Step {after_id} already has an outgoing connection (to step {existing_exit.get('toElement')}).",
                hint="Add this step without after_step_id, then use connect_steps ...",
            )

    element_id = tb.next_id([e.get("element_id") for e in elements])
    position = tb.compute_position(elements, after_id)
    create_entry = tb.build_element_create(step_type, element_id, clean_config, position)
    client.update_tree(workflow_id, elements=[create_entry])

    linked_from = None
    if after_id is not None:
        links = client.get_links(workflow_id)
        link_id = tb.next_id([l.get("link_id") for l in links])
        client.update_tree(workflow_id, links=[tb.build_link_create(link_id, after_id, element_id)])
        linked_from = str(after_id)

    return AddStepResult(step_id=str(element_id), type=step_type, linked_from=linked_from, warnings=warnings)
```
`after_step_id` koruması — anchor'ın zaten çıkan bir bağlantısı varsa
otomatik bağlamayı reddetmek — bir API kısıtlaması değil, kasıtlı bir
güvenlik seçimi. Kodun sadece ikinci bir link eklemesine engel olan
hiçbir şey yok; yapmama sebebi, birden fazla çıkışı olan bir step'in
**kasıtlı** kablolama gerektirmesi (bu bir if/else'in yeni bir dalı mı?
Bir split'ten paralel bir yol mu? Bunlar farklı ele alınmaları gerekir),
ve tahmin etmenin modelden `connect_steps` üzerinden açık olmasını
istemekten daha kötü olması. Bağlarken `update_tree`'nin **iki kez**
çağrıldığına dikkat et — bir kez element için, bir kez link için — tek
bir birleşik çağrıda değil; bu, daha net hata atfı için bir tasarım
seçimiydi (bağlama başarısız olursa, step yine de var ve hata bunu tam
olarak söylüyor, tüm işlemin atomik olarak başarısız olup modeli
workflow'un gerçekte hangi durumda olduğu konusunda belirsiz bırakmaması
yerine).

```python
@mcp.tool()
def connect_steps(workflow_id, from_step_id, to_step_id, outcome=""):
    source = client.get_element(workflow_id, from_step_id)
    source_type = source.get("type")
    is_branching = source_type in schema_registry.BRANCHING_TYPES

    if is_branching and not outcome:
        available = [o.get("conditionValue") for o in (source.get("outcomes") or [])]
        return ConnectStepsResult(error=f"{from_step_id} is a {source_type} and requires an outcome.",
                                  hint=f"Available outcomes: {available}")
    if not is_branching and outcome:
        return ConnectStepsResult(error=f"{from_step_id} ({source_type}) does not branch — it takes no outcome.")

    matched_outcome = None
    if is_branching:
        try:
            matched_outcome = tb.resolve_outcome(source, outcome)
        except tb.ValidationError as e:
            return ConnectStepsResult(error=str(e))

    links = client.get_links(workflow_id)
    link_id = tb.next_id([l.get("link_id") for l in links])
    client.update_tree(workflow_id, links=[tb.build_link_create(link_id, from_step_id, to_step_id)])

    if is_branching:
        try:
            client.update_tree(workflow_id, elements=[
                tb.build_outcome_update(source, matched_outcome["outcomeID"], link_id)
            ])
        except JotformAPIError as e:
            return ConnectStepsResult(
                link_id=str(link_id), from_step=from_step_id, to_step=to_step_id,
                error=f"Link created, but labelling the outcome failed: {e}. "
                      f"The steps are connected but the branch is unlabelled.",
            )

    return ConnectStepsResult(link_id=str(link_id), from_step=from_step_id,
                              to_step=to_step_id, outcome=outcome or None)
```
**Sıraya** dikkat et: link önce yazılıyor, outcome etiketi ikinci — ve
ikincisi başarısız olursa, hata açıkça bağlantının var olduğunu ama
etiketsiz olduğunu söylüyor, modelin hiçbir şey olmadığını varsaymasına
bırakmak yerine. Bu, dal kimliğinin gerçekten uçtan uca oluşturulduğu
tool — `tree_builder.resolve_outcome` ve `build_outcome_update`'teki
her şey tam olarak bu tek çağrı noktasına hizmet etmek için var.

`update_step`, dördü arasında en basiti: mevcut element'i, tipini
öğrenmek için çek (gerekli çünkü `validate_config`'in hangi şemaya karşı
kontrol edeceğini bilmesi lazım), yeni config'i doğrula, ve doğrulamayı
geçen bir şey varsa, sadece o alanlar için bir `action:"update"` gönder.
Kasıtlı olarak konum ya da bağlantılara **dokunmuyor** — bu
`connect_steps`'in işi, kasıtlı olarak ayrı tutuluyor.

---

### `tools/risky.py`

Üç tool — projedeki veriyi yok edebilen ya da yayınlayabilen tek
olanlar. Her biri aynı iki-çağrılı deseni uyguluyor.

```python
"""
Every tool here follows a two-call pattern: call once and nothing happens —
you get back what *would* happen. Call again with confirm=True and it does.
...
Why this shape and not a yes/no prompt inside the tool: MCP tools are
synchronous request/response, there's no channel to pause mid-call and wait
for a person to answer.
"""
```
Modül docstring'i mantığı doğrudan belirtiyor: MCP'nin bir tool
çağrısını duraklatıp bir insana soru sormak için yerleşik bir mekanizması
yok. İki ayrı tool çağrısını zorlamak, modelin ikinci çağrıyı
yapabilmeden önce **zaten önizlemeyi göstermiş ve konuşmada gerçek bir
cevap almış olması gerektiğini** garanti etmenin tek yolu — ilk çağrıda
`confirm=True` set eden bir model, teknik bir kısıtlamanın içinde
çalışmıyor, onay uyduruyor demektir.

```python
@mcp.tool()
def delete_step(workflow_id, step_id, confirm: bool = False):
    element = client.get_element(workflow_id, step_id)

    if not confirm:
        links = client.get_links(workflow_id)
        affected = []
        for link in links:
            if str(link.get("fromElement")) == str(step_id):
                affected.append(f"this step -> step {link.get('toElement')} will be broken")
            elif str(link.get("toElement")) == str(step_id):
                affected.append(f"step {link.get('fromElement')} -> this step will be broken")
        return DeleteStepResult(
            step_id=step_id, type=element.get("type"),
            label=element.get("name") or schema_registry.default_label(element.get("type")),
            needs_confirmation=True, affected_connections=affected,
            hint="Show this to the user. Call again with confirm=true only if they explicitly say to proceed.",
        )

    links = client.get_links(workflow_id)
    incident_link_ids = [l.get("link_id") for l in links
                         if str(l.get("fromElement")) == str(step_id)
                         or str(l.get("toElement")) == str(step_id)]
    link_deletes = [{"action": "delete", "linkID": lid, "data": {"link_id": lid}}
                    for lid in incident_link_ids]
    client.update_tree(
        workflow_id,
        elements=[{"action": "delete", "elementID": step_id, "data": {"element_id": step_id}}],
        links=link_deletes,
    )
    return DeleteStepResult(step_id=step_id, deleted=True)
```
`confirm=True` dalındaki link temizliği, doğrudan ölçülmüş bir bulgu
yüzünden var: bir element'i `updateTree` üzerinden silmek, linklerini
**kaskatlı silmiyor** (`probes/test_delete_impact.py`: start→A→B kuruldu,
A silindi, iki link de hayatta kaldı — biri artık var olmayan bir step'ten
işaret ediyor, biri buna işaret ediyor). Hedef step'e dokunan her link,
element ile **aynı `update_tree` çağrısında** siliniyor, yani API'nin
"element gitti" ile "linkler temizlendi" arasında kesintiye
uğrayabileceği bir pencere yok.

```python
@mcp.tool()
def publish_workflow(workflow_id, confirm: bool = False):
    combined = client.get_workflow_combined(workflow_id)
    if not confirm:
        ...
        health = graph.analyse(steps, conns)
        warnings = []
        if health["unreachable_steps"]: warnings.append(...)
        if health["dead_end_steps"]: warnings.append(...)
        if health["dangling_links"]: warnings.append(...)
        return PublishWorkflowResult(workflow_id=workflow_id, needs_confirmation=True,
                                     health_warnings=warnings, hint=...)
    client.publish_workflow(workflow_id)
    return PublishWorkflowResult(workflow_id=workflow_id, published=True)
```
Önizleme çağrısı sadece "emin misin" diye sormuyor — `get_workflow`'un
kullandığı **aynı** `graph.analyse` sağlık kontrolünü çalıştırıyor, ve
yapısal sorunları (ulaşılamayan step'ler, dead end'ler, kopuk linkler)
workflow canlıya geçmeden önce uyarı olarak ortaya çıkarıyor. Uyarısı
olan bir workflow yine de yayınlanmasına izin veriliyor — mesele
engellemek değil, kullanıcının bozuk bir daldan, o sessizce hiçbir yere
gitmeyen bir submission'dan öğrenmek yerine, canlıya geçmeden önce
asistandan haberdar olmasını sağlamak.

```python
@mcp.tool()
def delete_workflow(workflow_id, confirm: bool = False, confirm_title: str = ""):
    meta = client.get_workflow(workflow_id)
    title = meta.get("title")

    if not confirm:
        return DeleteWorkflowResult(
            workflow_id=workflow_id, title=title, needs_confirmation=True,
            hint=f"Show the title '{title}' to the user. Call again with confirm=true "
                 f"and confirm_title='{title}' only if they explicitly confirm THIS workflow, by name.",
        )
    if confirm_title != title:
        return DeleteWorkflowResult(
            workflow_id=workflow_id, title=title,
            error=f"confirm_title ({confirm_title!r}) does not match this workflow's actual title ({title!r}). Not deleted.",
        )
    client.delete_workflow(workflow_id)
    return DeleteWorkflowResult(workflow_id=workflow_id, title=title, deleted=True)
```
Projedeki, düz `confirm=True`'dan daha katı korumaya sahip **tek**
tool — ayrıca `confirm_title`'ın workflow'un gerçek başlığıyla tam
eşleşmesini istiyor. Bu, test sırasında yaşanan gerçek bir kıl payı
olay yüzünden var: gerçek (atılabilir olmayan) bir workflow, gerçek ve
atılabilir workflow'ları karıştıran numaralı bir listeden yanlış girdi
seçilerek yanlışlıkla silindi. Sadece `confirm=True`, yalnızca *bir*
onayın gerçekleştiğini kanıtlıyor — *doğru* hedefin onaylandığını
kanıtlamıyor. Başlığı zorlamak, bu spesifik tool işlem yapabilmeden
önce modelin gerçek adı, sadece bir id değil, ortaya çıkarmış olmasını
sağlıyor. `delete_step` kasıtlı olarak daha basit desende bırakıldı —
bir step yeniden inşa edilerek kurtarılabilir; tüm bir workflow çok
daha büyük, kurtarılması daha zor bir kayıp, ve bu ekstra sürtünmeyi hak
ediyor.

---

### `server.py`

```python
"""
Jotform Workflow MCP server.
...
Tool layers:
  1. discovery — list_step_types, get_step_schema
  2. reading   — list_workflows, get_workflow, get_step_details, list_forms, get_form_fields
  3. building  — create_workflow, add_step, connect_steps, update_step
  4. risky     — delete_step, publish_workflow, delete_workflow (confirm=True required to act)
"""
from dotenv import load_dotenv
from mcp.server import MCPServer

load_dotenv()

from mcp_server.jotform_client import JotformClient  # noqa: E402
from mcp_server.tools import building, discovery, reading, risky  # noqa: E402

mcp = MCPServer("jotform-workflow")
client = JotformClient()

discovery.register(mcp)
reading.register(mcp, client)
building.register(mcp, client)
risky.register(mcp, client)

if __name__ == "__main__":
    mcp.run()
```
30 satır, ve her biri yerini hak ediyor. `load_dotenv()`,
`from mcp_server.jotform_client import JotformClient` satırından
**önce** çalışıyor — bu sıra kozmetik değil. `jotform_client.py`,
`JOTFORM_API_KEY`'i **modül import anında** environment'tan okuyor
(o dosyanın en üstündeki `BASE_URL = os.environ.get(...)`), yani
import'lar önce olsaydı, environment değişkenleri henüz var olmazdı ve
client boş bir key ile construct edilirdi. `# noqa: E402` yorumları,
linter'ın "import'lar dosyanın en üstünde olmalı" kuralını susturuyor —
tam olarak bu sebeple kasıtlı olarak burada kırılıyor. `client =
JotformClient()`, modül scope'unda **bir kez** construct ediliyor, ve
her `register()` çağrısına geçiriliyor — bu yüzden eksik bir API key,
server'ı boot'ta hemen öldürüyor (yukarıdaki `jotform_client.py`'nin
`__init__`'ine bak) her tool ayrı ayrı ve kafa karıştırıcı şekilde daha
sonra başarısız olması yerine. Her katmanın `register(mcp, client)`'ı
kendi tool'larını aynı paylaşılan `mcp` örneğine ekliyor; katmanlama
tamamen dosya-organizasyonu seviyesinde var, `server.py`'nin kendisi
katman sınırlarını zorlamıyor, hatta bilmiyor bile.

---

## `tests/` — gerçekte ne kanıtlanmış

Toplam 37 test, `python -m pytest tests/ -q` ile çalıştırılıyor, ağ yok,
API key yok, iki saniyeden az sürede tamamlanıyor.

**`tests/test_graph.py`** (13 test) — fixture'lar, bu projenin ilk
incelediği gerçek 18-step workflow'u içeriyor, yani buradaki bir
regresyon, gözle bir kez zaten doğrulanmış bir sayıdaki değişiklik olarak
ortaya çıkıyor. Kapsadıkları: gerçek veri üzerinde doğru orphan/dead-end
sayımı, end-point'lerin yanlışlıkla dead end diye işaretlenmemesi,
döngülerin sonsuz döngüye yol açmaması, karışık int/string step id'lerinin
doğru karşılaştırılması, outcome-eşleme mantığı (`outcomes[].linkID`'yi
bağlantılara eşlemek, bağlanmamış dalları ele almak, `workflow_split` gibi
dallanmayan tipleri görmezden gelmek), ve dangling-link tespitçisi.

**`tests/test_tree_builder.py`** (24 test) — id atama, layout fallback
davranışı, config doğrulama (bilinmeyen alanlar düşürülüyor, enum
ihlalleri reddediliyor, konumlandırma/kimlik alanları her zaman
sıyrılıyor), ölçülmüş link payload şeklinin tam eşleşmesi (geleceğe
yönelik bir `LINK_DEFAULTS` düzenlemesinin, gerçek API'ye karşı
başarısız olmadan önce bir testi başarısız etmesi için sabitlendi),
outcome çözümleme (büyük/küçük harf duyarsız eşleşme, bilinmeyen-outcome
hatası, zaten-bağlı hatası), diğer outcome'ları koruyan outcome-güncelleme
payload'ı, ve varsayılan-outcome enjeksiyonu (binary decision'lar
TRUE/FALSE alıyor, conditional branch'ler varsayılan kovalarını alıyor,
dallanmayan tipler hiçbir şey almıyor, çağıran-sağlanan outcome'lar
üzerine yazılmadan saygı görüyor).

---

## `probes/` — her biri tek satır

Bunlar ürünün parçası değil. `docs/gap-report.md` ve
`docs/decision-log.md`'deki her iddianın gerçekte nasıl kurulduğu bu.
Hiçbirinin kodunun ezberlenmesine gerek yok — sadece her birinin neyi
kontrol ettiği ve neyi bulduğu.

### Bu fazdan (Faz 1-4), regresyon kontrolü olarak tekrar kullanılabilir

| Script | Kontrol ettiği | Bulduğu |
|---|---|---|
| `smoke_test.py` | 14 tool'un tümü, mutlu yol, ~10 saniyede | Herhangi bir değişiklikten sonra çalıştırılacak hızlı sağlık kontrolü |
| `inspect_links.py` | Her link objesindeki ham alanlar | `labels` her zaman boş; `fromPortName` tuval geometrisi, dal kimliği değil |
| `inspect_outcomes.py` | `/combined`'ın element'lerde `outcomes` içerip içermediği | Evet — ekstra çağrı gerekmiyor |
| `test_link_ports.py` / `test_link_ports2.py` | Bir link yazmanın gerçekte hangi alanları gerektirdiği, ve değerlerin doğrulanıp doğrulanmadığı | `points` boş olmayan içerik istiyor (görmezden geliniyor); portlar doğrulanmıyor ve kendini düzeltiyor; `type` doğrulanmıyor ve **düzeltilmiyor** |
| `test_outcome_write.py` | `outcomes[].linkID`'nin `action:"update"` ile, geri okumayla, set edilip edilemediği | Evet, uçtan uca doğrulandı |
| `test_write_path.py` | Workflow oluştur / element oluştur / link oluştur / element sil, her biri geri okunarak | Hepsi çalıştığı doğrulandı |
| `test_building_tools.py` | Katman 3'ün tümü, `mcp.call_tool` üzerinden, mutlu yol ve beş kasıtlı başarısızlık modu | Her şey geçti; bu koşu, varsayılan-outcomes bug'ını ilk ortaya çıkaran koşuydu |
| `test_delete_impact.py` | Bir element silmek linklerini kaskatlı siliyor mu? | **Hayır** — linkler hayatta kalıyor, hiçliğe işaret ederek |
| `test_delete_workflow.py` | `DELETE /workflow/{id}` çalışıyor mu? Ayrıca bir temizlik aracı (`--sweep-probes`) | Evet, doğrulandı ve kalıcı |
| `test_noop_updatetree_effect.py` | Boş bir `updateTree` çağrısı, bir yan etki olarak bir şeyi değiştiriyor mu? | Hayır — güvenli |
| `test_set_trigger_form.py` / `inspect_trigger_binding.py` | `setResource` gerçekten bir tetikleyici formu bağlıyor mu? | **Hayır** — sessiz no-op olarak doğrulandı, öncesi/sonrası her alan karşılaştırılarak |
| `test_publish_workflow.py` | `publish_workflow` gerçekten çalışıyor mu? | Endpoint kabul ediyor ve `live: 1` döndürüyor, ama `publishStatus`/`hasPublishedFlow` güvenilir doğrulama sinyalleri değil |

### Faz 0'dan (bu oturumdan önce, orijinal keşif)

| Script | Kontrol ettiği |
|---|---|
| `client.py` | Paylaşılan harness — her Faz 0 probe'u bunun üzerinden `probes/findings/*.jsonl`'a log yazıyor (şu an boş — aşağıdaki nota bak) |
| `discover_from_official_sdk.py` | Yetkili bir endpoint listesi almak için Jotform'un resmi Python SDK kaynağını çekti. Bulunan: 47 endpoint'in sıfırı "workflow" içermiyor — bu projedeki workflow'la ilgili her şey, resmi olarak onaylanmış değil, ampirik olarak keşfedilmiş |
| `run_full_sweep.py` | O listeden her **GET** endpoint'ini otomatik tarıyor; mutating olanlar atlandı diye loglanıyor, hiç otomatik ateşlenmiyor |
| `run_public_api.py` / `run_internal_bff.py` | İki yüzeyin (`api.jotform.com` vs `www.jotform.com/API`) farklı davrandığını doğruluyor — internal olan, tarayıcı dışından CSRF ile bloklanmış |
| `phase0_close_gaps.py` | `list_workflows`'u bulan, `/combined`'ı doğrulayan, element silmeyi doğrulayan orijinal script |
| `explore_workflow_surface.py` / `explore_templates.py` | Makul-ama-denenmemiş yolların keşif taraması — çoğunlukla 404, birkaç gerçek bulgu |
| `inspect_workflow_elements.py` / `inspect_workflow_links.py` / `dump_specific_elements.py` | Manuel inceleme için ham, kırpılmamış API response'larını dosyalara döküyor |
| `compare_conditional_branch_types.py` | `workflow_conditional_branch`'in tam config'ini `workflow_binary_decision`'ınkiyle karşılaştırdı |
| `fresh_workflow_and_type_test.py` | Element tipi kalıcılığını test etmek için temiz bir scratch workflow kurdu |
| `verify_pdf_reference.py` | İç bir referans dokümandaki her iddiayı, olduğu gibi güvenmek yerine, canlı API'ye karşı tekrar kontrol etti |
| `test_elements_write.py` / `discover_element_schema.py` | `build_branching_workflow.py`'nin daha sonra kullandığı **alternatif** yazma yolunu (`POST /workflow/{id}/elements`) araştırdı. **Not:** `probes/findings/` boş — bu spesifik script'lerin canlı API'ye karşı başarısı muhtemelen hiç bağımsız olarak loglanmadı; bu projenin kendi `updateTree`-tabanlı bulgularının yanında eşit derecede doğrulanmış değil, makul, HAR-destekli bir hipotez olarak muamele et |
| `build_workflow_from_scratch.py` / `build_polished_demo_workflow.py` / `build_branching_workflow.py` | `POST /elements` yolunu kullanan uçtan uca demo build'leri. Bu projenin ulaştığı **aynı** `type: "default-link"` kuralına ve **aynı** `outcomes[].linkID` dal mekanizmasına bağımsız olarak ulaştı — tamamen farklı bir keşif rotasından, iyi bir çapraz doğrulama |

---

## `docs/` — hikâyenin yaşadığı yer

- **`gap-report.md`** — canlı kapasite matrisi: bu projede kullanılan
  her endpoint, çalıştığı doğrulandı mı, ve tam olarak nasıl doğrulandı.
  "Bu ürün X'i yapabilir mi" sorusunun cevabı için bunu oku.
- **`decision-log.md`** — her önemsiz görünmeyen kararın, tarihli,
  düşünülen alternatifle ve neden kaybettiğiyle birlikte kaydı — ilk
  denemede *yanlış* olanlar ve bunun nasıl yakalandığı dahil. "Kod neden
  bu şekilde yapılıyor" sorusunun cevabı için bunu oku.

---

## Tasarımı şekillendiren bulgular

Kısa versiyon, bir mentor "yol boyunca gerçekte ne ters gitti" diye
sorarsa:

1. **Dal kimliği linkte değil.** İlk tahmin (port isimleri) makuldü ve
   test edilen tek workflow'da tesadüfen doğruydu, ki bu bug olarak
   çıkardı olurdu. Gerçek cevap — karar veren element'teki
   `outcomes[].linkID` — aynı örnek üzerinde daha sert test etmekten
   değil, Jotform'un kendi şemasını okumaktan geldi.
2. **Bir link yazmanın, API'nin dokümante etmediği dört alana ihtiyacı
   var**, ve bunlardan biri (`type`) hardcoded bir sabit olmak zorunda
   çünkü API oradaki bir yazım hatasını sessizce kabul edip saklıyor,
   diğer üçü (`points`, port isimleri) kendini düzeltiyor ya da önemli
   değil.
3. **Bir şemanın deklare ettiği `default`, sunucu tarafından
   uygulanmıyor.** Açık `outcomes` olmadan oluşturulan bir if/else
   kalıcı olarak bağlanamaz durumda.
4. **Silmeler kaskatlanmıyor.** Bir element'i silmek, linklerini arkada
   bırakıyor, hiçliğe işaret ederek.
5. **`setResource`, public API'de sessiz bir no-op** — en tehlikeli
   başarısızlık türü, çünkü başarı raporluyor.
6. **Boolean status bayrakları yalan söylüyor.** `hasAnyWorkflow` ve
   `hasPublishedFlow`'un ikisi de, adlandırılan koşulun yanlış olduğu
   kayıtlarda `true` gözlemlendi. Bu API'den hiçbir boolean alan artık
   sadece isme bakarak güvenilmiyor — her biri kullanılmadan önce açık
   bir true/false kontrolü gerektirdi.
7. **Bir kıl payı silme olayı** (gerçek ve atılabilir veriyi karıştıran
   bir listeden yanlış girdi seçilerek gerçek bir workflow'un silinmesi)
   doğrudan `delete_workflow`'un başlık-doğrulama gereksinimini üretti.

---

## Server'ı çalıştırmak

```bash
cp .env.example .env      # JOTFORM_API_KEY'i doldur
pip install -r requirements.txt
python -m pytest tests/ -q          # 37 test, ağ yok, hepsi geçmeli
python -m mcp_server.server         # stdio server'ı boot ediyor
```

Ya da launcher üzerinden (MCP client'larının sık düştüğü working-directory
sorunlarını hallediyor):

```bash
./run_server.sh
```

Yerel bir Claude Desktop'a bağlamak için: `claude_desktop_config.json`'a,
`command`'ı `run_server.sh`'in mutlak yoluna işaret eden bir girdi ekle.
Uzak connector'lar (senin makinenden değil, Anthropic'in bulutundan
erişilen) ayrı bir deployment sorusu — bu durum için auth modelinin
nasıl değiştiğine dair not için gap-report.md'ye bak (server sadece
senin kontrolünde olmaktan çıktığında, tek paylaşılan `.env` key'i işe
yaramıyor).

## Şu anki durum

4 katmana yayılmış 14 tool. 12'si read-back doğrulamasıyla uçtan uca
tam olarak doğrulandı. 1'i (`publish_workflow`) çalışıyor ama en iyi
doğrulama sinyali hâlâ açık bir soru. 1 tanesi bilinen, doğrulanmış,
kalıcı bir kısıtlama (`create_workflow`'un `trigger_form_id`'si —
altındaki API çağrısı bir no-op; tool artık bunu tespit edip yanlış
başarı iddia etmek yerine raporluyor).

Şu an server'ı kullanmayı bloke eden hiçbir açık madde yok. Kalan
eksikler (birkaç step tipi için şema/UI-adı kapsamı, özel isimli
conditional branch'ler, gerçek tuval auto-layout'u, uzak bir connector
için deployment/auth modeli) sırada ne olduğuyla ilgili kapsam soruları,
var olanın içindeki kusurlar değil.