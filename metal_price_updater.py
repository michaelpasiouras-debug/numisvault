import requests
from supabase import create_client, Client
import urllib3

# Απενεργοποίηση προειδοποιήσεων SSL για καθαρό τερματικό
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ======================================================================
# 🔴 ΣΤΟΙΧΕΙΑ ΣΥΝΔΕΣΗΣ (Δώστε τα στον προγραμματιστή σας για την παραγωγή)
# ======================================================================
SUPABASE_URL = "https://supabase.co"
SUPABASE_KEY = "your-service-role-key" 

# 🔴 ΔΩΡΕΑΝ ΚΛΕΙΔΙ ΑΠΟ ΤΟ goldapi.io (Χρησιμοποιείστε το δικό σας)
GOLD_API_KEY = "goldapi-your-free-key-here"

# Αρχικοποίηση σύνδεσης με το Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"[-] Αποτυχία αρχικοποίησης Supabase Client: {e}")

def fetch_and_update_metal_prices():
    grams_per_ounce = 31.1035
    metals_to_update = {
        "gold": "XAU",
        "silver": "XAG"
    }
    
    print("\n[*] Έναρξη άντλησης live χρηματιστηριακών τιμών (EUR)...")
    
    for metal_name, metal_symbol in metals_to_update.items():
        url = f"https://goldapi.io{metal_symbol}/EUR"
        headers = {
            "x-access-token": GOLD_API_KEY,
            "Content-Type": "application/json"
        }
        
        try:
            # Live κλήση στο χρηματιστήριο πολύτιμων μετάλλων
            response = requests.get(url, headers=headers, timeout=12, verify=False)
            
            if response.status_code == 200:
                data = response.json()
                
                # Λήψη τιμής ανά Ουγγιά (troy ounce)
                price_per_ounce_eur = float(data.get("price"))
                
                # 📊 ΜΑΘΗΜΑΤΙΚΟΣ ΤΥΠΟΣ: Μετατροπή Ουγγιάς σε Γραμμάριο
                price_per_gram_eur = price_per_ounce_eur / grams_per_ounce
                
                print(f"[+] {metal_name.upper()} LIVE: {price_per_ounce_eur:.2f} €/oz -> {price_per_gram_eur:.4f} €/g")
                
                # Προετοιμασία εγγραφής για το Supabase
                update_data = {
                    "metal_type": metal_name,
                    "price_per_gram": price_per_gram_eur
                }
                
                # Αποστολή και ενημέρωση (Upsert) στον πίνακα metal_prices
                supabase.table("metal_prices").upsert(update_data).execute()
                print(f"[✓] Ο πίνακας 'metal_prices' ενημερώθηκε για το μέταλλο: {metal_name}")
                
            else:
                # Αν δεν έχετε βάλει ακόμα σωστά κλειδιά, το script θα τρέξει σε λειτουργία "Δοκιμής" (Demo Mode)
                print(f"[-] Status {response.status_code}. Ενεργοποίηση Demo Mode για το μέταλλο {metal_name}...")
                run_demo_mode(metal_name, grams_per_ounce)
                
        except Exception as e:
            print(f"[-] Σφάλμα δικτύου. Ενεργοποίηση Demo Mode για το μέταλλο {metal_name}...")
            run_demo_mode(metal_name, grams_per_ounce)

def run_demo_mode(metal_name, grams_per_ounce):
    """Λειτουργεί ως δικλείδα ασφαλείας αν δεν υπάρχει σύνδεση ίντερνετ ή API key, χρησιμοποιώντας τις δικές σας τιμές."""
    # BUG 17: safer realistic demo baseline for silver.
    mock_ounce_prices = {"silver": 30.00, "gold": 4400.00}
    price_per_ounce = mock_ounce_prices[metal_name]
    price_per_gram = price_per_ounce / grams_per_ounce
    print(f"[💡 DEMO MODE] {metal_name.upper()}: {price_per_ounce:.2f} €/oz -> {price_per_gram:.4f} €/g")
    print("[✓] Προσομοίωση ολοκληρώθηκε με επιτυχία.")

if __name__ == "__main__":
    fetch_and_update_metal_prices()