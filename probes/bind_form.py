import os
from dotenv import load_dotenv
from mcp_server.jotform_client import JotformClient, JotformAPIError

def main():
    # .env dosyasındaki JOTFORM_API_KEY bilgisini yükler
    load_dotenv()
    client = JotformClient()
    
    workflow_id = "262213961993969"
    form_id = "262143531696055"

    print(f"Workflow ID : {workflow_id}")
    print(f"Hedef Form ID: {form_id}")
    print("-" * 50)

    # 1. API'ye bağlama isteğini at
    print("setResource isteği atılıyor...")
    try:
        result = client.set_trigger_form(workflow_id, form_id)
        print(f"API Yanıtı: {result}")
    except JotformAPIError as e:
        print(f"API Hatası: {e}")
        return

    # 2. Değişikliğin işe yarayıp yaramadığını doğrula (Read-back)
    print("\nDeğişiklik kontrol ediliyor (/combined üzerinden)...")
    try:
        combined = client.get_workflow_combined(workflow_id)
        start_point = next(
            (e for e in (combined.get("elements") or []) 
             if isinstance(e, dict) and e.get("type") == "workflow_start_point"),
            {}
        )
        
        current_resource_id = str(start_point.get("resourceID", ""))
        print(f"Okunan start_point resourceID: {current_resource_id}")
        
        if current_resource_id == form_id:
            print("\n[BAŞARILI] Form trigger olarak workflow'a bağlandı.")
        else:
            print("\n[BAŞARISIZ] API başarılı dönmüş olsa da resourceID güncellenmemiş (Silent no-op).")
            print("Gerçek element dump'ı:")
            print(start_point)
            
    except JotformAPIError as e:
        print(f"Okuma sırasında hata: {e}")

if __name__ == "__main__":
    main()