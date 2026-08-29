import os
import re
import time
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ======================================================================
# 🔴 ΣΤΟΙΧΕΙΑ ΣΥΝΔΕΣΗΣ SUPABASE (Θα συμπληρωθούν από τον προγραμματιστή)
# ======================================================================
SUPABASE_URL = "https://supabase.co"
SUPABASE_KEY = "your-service-role-key" # Απαιτείται το service_role key για δικαιώματα εγγραφής & upload

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"[-] Αποτυχία αρχικοποίησης Supabase Client: {e}")

def get_next_target_id():
    """Βρίσκει το επόμενο ID νομίσματος που πρέπει να κατεβεί, ελέγχοντας το μέγιστο ID στον πίνακα numista_catalog."""
    try:
        response = supabase.table("numista_catalog").select("id").order("id", desc=True).limit(1).execute()
        if response.data:
            return response.data[0]["id"] + 1
        return 1 # Ξεκινάει από το ID 1 αν ο πίνακας είναι εντελώς άδειος
    except Exception as e:
        print(f"[-] Αποτυχία ελέγχου ID από το Supabase: {e}")
        return None

def upload_image_to_supabase(url, coin_id, side):
    """Κατεβάζει την εικόνα από το Numista και την ανεβάζει στο δικό σας Supabase Bucket."""
    if not url or url == "https://numista.com":
        return None
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        # Λήψη εικόνας με παράκαμψη SSL
        res = requests.get(url, headers=headers, timeout=12, verify=False)
        if res.status_code == 200:
            file_name = f"{coin_id}_{side}.jpg"
            bucket_name = "coin-images" # Το Public Bucket στο Supabase Storage
            
            # Μεταφόρτωση στο Supabase Storage
            supabase.storage.from_(bucket_name).upload(
                path=file_name,
                file=res.content,
                file_options={"content-type": "image/jpeg", "upsert": "true"}
            )
            
            # Παραγωγή του δικού σας Public URL που δεν μπλοκάρεται από hotlinking
            public_url = supabase.storage.from_(bucket_name).get_public_url(file_name)
            return public_url
    except Exception as e:
        print(f"[-] Αποτυχία μεταφόρτωσης εικόνας ({side}) για το ID {coin_id}: {e}")
    return None

def scrape_and_seed_coin(coin_id):
    """Πραγματοποιεί Web Scraping στο Numista ID και αποθηκεύει τα δεδομένα στον πίνακα numista_catalog."""
    coin_url = f"https://numista.com{coin_id}.html"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    print(f"[+] Έναρξη επεξεργασίας: ID {coin_id} -> {coin_url}")
    
    try:
        response = requests.get(coin_url, headers=headers, timeout=12)
        if response.status_code == 404:
            print(f"[-] Το ID {coin_id} δεν επιστρέφει σελίδα (404). Μετάβαση στο επόμενο.")
            # Αποθηκεύουμε ένα κενό record για να μην ξαναχτυπήσει το script αυτό το ID
            empty_data = {"id": coin_id, "title": "N/A - 404", "country": "Unknown"}
            supabase.table("numista_catalog").upsert(empty_data).execute()
            return False
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            h1_tag = soup.find('h1')
            if not h1_tag:
                return False
            
            title = h1_tag.text.strip()
            page_text = soup.get_text()
            
            # Εξαγωγή τεχνικών χαρακτηριστικών (Regex)
            weight_match = re.search(r'Weight\s*([\d.]+)\s*g', page_text)
            diameter_match = re.search(r'Diameter\s*([\d.]+)\s*mm', page_text)
            purity_match = re.search(r'Composition\s*.*?([\d.]+)', page_text)
            
            weight = float(weight_match.group(1)) if weight_match else None
            diameter = float(diameter_match.group(1)) if diameter_match else None
            purity = float(purity_match.group(1)) if purity_match else None
            
            # Καθορισμός μετάλλου
            composition = "Base Metal"
            if "silver" in page_text.lower():
                composition = "Silver"
            elif "gold" in page_text.lower():
                composition = "Gold"
                
            # Έλεγχος χώρας
            country_tag = soup.find('a', href=re.compile(r'/catalogue/greece'))
            country = "Greece" if country_tag else "International"
            
            # Εντοπισμός φωτογραφιών
            img_tags = soup.find_all('img', src=re.compile(r'/catalogue/photos/'))
            raw_obv = "https://numista.com" + img_tags[0]['src'] if len(img_tags) > 0 else None
            raw_rev = "https://numista.com" + img_tags[1]['src'] if len(img_tags) > 1 else None
            
            # Ανέβασμα των φωτογραφιών στο δικό σας cloud storage
            img_obverse_public = upload_image_to_supabase(raw_obv, coin_id, "obverse")
            img_reverse_public = upload_image_to_supabase(raw_rev, coin_id, "reverse")
            
            coin_data = {
                "id": coin_id,
                "title": title,
                "country": country,
                "year_era": "Catalogued",
                "weight": weight,
                "diameter": diameter,
                "composition": composition,
                "purity": purity,
                "img_obverse": img_obverse_public,
                "img_reverse": img_reverse_public
            }
            
            # Αποθήκευση στον πίνακα numista_catalog
            supabase.table("numista_catalog").upsert(coin_data).execute()
            print(f"[✓] Επιτυχής καταγραφή: {title} -> Αποθηκεύτηκε στο Supabase.")
            return True
            
    except Exception as e:
        print(f"[-] Σφάλμα κατά το Scraping του ID {coin_id}: {e}")
    return False

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    print("[*] Εκκίνηση Background Seeding Worker για τον πίνακα 'numista_catalog'...")
    
    next_id = get_next_target_id()
    if next_id:
        scrape_and_seed_coin(next_id)
