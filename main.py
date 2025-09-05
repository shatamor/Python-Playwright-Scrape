# KODUN TAMAMI YENİ HALLERİYLE
import discord
import os
from dotenv import load_dotenv
load_dotenv()
import requests
import json
from flask import Flask
from threading import Thread
from playwright.async_api import async_playwright
import asyncio
import time
import re
import logging
from datetime import datetime
import httpx
from bs4 import BeautifulSoup

# --- YENİ: Debug ve Hata Ayıklama Kurulumu ---
if not os.path.exists('debug_output'):
    os.makedirs('debug_output')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    handlers=[
        logging.FileHandler("debug_output/bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# --- Global Değişkenler ---
playwright = None
browser = None
currency_cache = { "rate": None, "last_fetched": 0 }
ITAD_API_KEY = os.environ.get('ITAD_API_KEY')

# --- Web Sunucusu ve Keep Alive ---
app = Flask('')
@app.route('/')
def home(): return "Bot Aktif ve Çalışıyor!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Oyun Adı Temizleme Fonksiyonu (FİNAL VERSİYON: ™, ®, © sembolleri eklendi) ---
def clean_game_name(game_name):
    # Romen rakamlarını sayılara çevir, orijinal metni koru
    name_with_arabic, _ = clean_and_extract_roman(game_name)

    # 1. Adım: TM, R, C ve kesme işaretlerini tamamen kaldır.
    # Bu, "The Last of Us™" -> "The Last of Us" olmasını sağlar.
    cleaned_name = name_with_arabic.replace("™", "")
    cleaned_name = cleaned_name.replace("®", "")
    cleaned_name = cleaned_name.replace("©", "")
    cleaned_name = cleaned_name.replace("'", "")
    cleaned_name = cleaned_name.replace("’", "") # Farklı bir kesme işareti tipi

    # 2. Adım: Kalan özel karakterleri (harf, rakam veya boşluk olmayan her şeyi) boşlukla değiştir.
    cleaned_name = re.sub(r'[^\w\s]', ' ', cleaned_name, flags=re.UNICODE)

    # 3. Adım: Oluşabilecek çoklu boşlukları tek boşluğa indir.
    cleaned_name = re.sub(r'\s+', ' ', cleaned_name)

    return cleaned_name.strip().lower()

# --- YENİ: Romen Rakamı ve Sayı Çıkarma Yardımcıları ---
def clean_and_extract_roman(name):
    """Converts Roman numerals at the end of a string to Arabic numerals."""
    name = name.upper()
    roman_map = {'I': 1, 'V': 5, 'X': 10}
    replacements = {'IV': '4', 'IX': '9', 'V': '5', 'I': '1'} # Order matters

    # Check for specific roman numerals at the end
    if name.endswith(" IV"): return name.replace(" IV", " 4"), 4
    if name.endswith(" IX"): return name.replace(" IX", " 9"), 9
    if name.endswith(" V"): return name.replace(" V", " 5"), 5
    if name.endswith(" III"): return name.replace(" III", " 3"), 3
    if name.endswith(" II"): return name.replace(" II", " 2"), 2
    if name.endswith(" I"): return name.replace(" I", " 1"), 1

    return name.lower(), None # Return original cleaned name if no roman numeral

def extract_numbers_from_title(title):
    """Extracts all Arabic and Roman numerals from a game title."""
    # First, find all standard digits
    numbers = set(map(int, re.findall(r'\d+', title)))

    # Then, check for Roman numerals which are often used for sequels
    # We check for them as standalone words to avoid matching 'I' in 'is'
    # Using uppercase for consistency
    title_upper = f" {title.upper()} "
    if " II " in title_upper or title_upper.endswith(" II"): numbers.add(2)
    if " III " in title_upper or title_upper.endswith(" III"): numbers.add(3)
    if " IV " in title_upper or title_upper.endswith(" IV"): numbers.add(4)
    if " V " in title_upper or title_upper.endswith(" V"): numbers.add(5)

    return numbers

# --- Döviz Kuru Alma Fonksiyonu ---
def get_usd_to_try_rate():
    global currency_cache
    if time.time() - currency_cache["last_fetched"] > 3600:
        try:
            response = requests.get("https://api.frankfurter.app/latest?from=USD&to=TRY")
            if response.status_code == 200:
                rate = response.json().get("rates", {}).get("TRY")
                if rate:
                    currency_cache["rate"] = rate
                    currency_cache["last_fetched"] = time.time()
                    logging.info(f"Yeni USD/TRY kuru alındı: {rate}")
                    return rate
            logging.warning(f"Döviz kuru alınamadı. Status Code: {response.status_code}")
            return currency_cache["rate"]
        except Exception as e:
            logging.error(f"Döviz kuru alınırken hata: {e}")
            return currency_cache["rate"]
    else: return currency_cache["rate"]

# --- Steam Fiyat ve Link Alma Fonksiyonu (YENİ: Akıllı Puanlama Sistemiyle) ---
def get_steam_price(game_name):
    try:
        # 1. Kullanıcının arama terimindeki sayıyı bul
        user_query_numbers = extract_numbers_from_title(game_name)
        # Eğer kullanıcı 'Red Dead Redemption' yazdıysa bu set boş olacak.
        # Eğer 'Red Dead Redemption 2' yazdıysa {2} olacak.

        search_url = f"https://store.steampowered.com/api/storesearch/?term={requests.utils.quote(game_name)}&l=turkish&cc=TR"
        response = requests.get(search_url)
        if response.status_code != 200 or not response.json().get('items'):
            logging.warning(f"Steam araması başarısız oldu. Status Code: {response.status_code}, Game: {game_name}")
            return None

        search_results = response.json().get('items', [])
        if not search_results:
            logging.info(f"Steam'de '{game_name}' için sonuç bulunamadı.")
            return None

        best_match = None
        highest_score = -1

        # 2. Tüm sonuçları gez ve puanla
        for item in search_results:
            item_name = item.get('name', '')
            cleaned_item_name = clean_game_name(item_name)

            # Puanlama Başlangıcı
            current_score = 0

            # Metinsel Benzerlik Puanı (Temel Puan)
            # Bu, "Bioshock" ile "Bioshock Remastered" eşleşmesini sağlar.
            # rapidfuzz kütüphanesi bu iş için harikadır ama basit bir `in` kontrolü de iş görür.
            # Daha basit ve hatasız olması için `in` kullanalım.
            if game_name in cleaned_item_name:
                current_score += 90
            elif cleaned_item_name in game_name:
                current_score += 85
            else: # Eğer temel isim bile eşleşmiyorsa, bu sonucu atla
                continue

            # Sayısal Eşleşme Puanı (Filtreleme)
            result_numbers = extract_numbers_from_title(cleaned_item_name)

            if user_query_numbers: # Kullanıcı bir sayı belirtti (örn: RDR 2)
                if not user_query_numbers.intersection(result_numbers):
                    current_score -= 100 # Yanlış devam oyunu, puanı düşürerek ele
            else: # Kullanıcı sayı belirtmedi (örn: RDR)
                # Sonuçta 1'den büyük bir sayı varsa (örn: RDR 2), bu istenmeyen bir devam oyunudur.
                if any(n > 1 for n in result_numbers):
                    current_score -= 100 # İstenmeyen devam oyunu, puanı düşürerek ele

            # En yüksek skorlu sonucu sakla
            if current_score > highest_score:
                highest_score = current_score
                best_match = item

        # 3. Yeterince iyi bir eşleşme bulunduysa devam et
        if not best_match or highest_score < 50:
             logging.info(f"Steam'de '{game_name}' için yeterli doğrulukta bir eşleşme bulunamadı.")
             return None

        link = f"https://store.steampowered.com/app/{best_match.get('id')}"
        game_name_from_steam = best_match.get('name')
        price_data = best_match.get('price')

        if not price_data:
            if best_match.get('unpurchaseable'):
                 return {"price": "Fiyat bilgisi yok.", "link": link, "name": game_name_from_steam}
            else:
                 return {"price": "Ücretsiz!", "link": link, "name": game_name_from_steam}

        price_float = None
        if isinstance(price_data, dict):
            final_price = price_data.get('final')
            if isinstance(final_price, int):
                price_float = final_price / 100.0

        if price_float is not None:
            return {"price": (price_float, "USD"), "link": link, "name": game_name_from_steam}
        else:
            return {"price": "Fiyat bilgisi yok.", "link": link, "name": game_name_from_steam}

    except Exception as e:
        logging.error(f"STEAM HATA: {e}", exc_info=True)
        return None

# YENİ: Hata durumunda ekran görüntüsü VE HTML KAYDEDEN yardımcı fonksiyon
async def take_screenshot_on_error(page, platform_name, game_name):
    if not page or page.is_closed():
        logging.warning(f"{platform_name} için hata ayıklama verisi kaydedilemedi, sayfa kapalı.")
        return

    try:
        # Dosyalar için benzersiz bir zaman damgası oluştur
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_game_name = re.sub(r'[^\w-]', '_', game_name) # Dosya adında sorun yaratacak karakterleri temizle

        # 1. Ekran Görüntüsünü Kaydet
        screenshot_path = f"debug_output/error_{platform_name}_{safe_game_name}_{timestamp}.png"
        await page.screenshot(path=screenshot_path)
        logging.info(f"Hata ekran görüntüsü kaydedildi: {screenshot_path}")

        # 2. Sayfanın HTML İçeriğini Kaydet
        html_path = f"debug_output/error_{platform_name}_{safe_game_name}_{timestamp}.html"
        html_content = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logging.info(f"Hata anındaki sayfa HTML'i kaydedildi: {html_path}")

    except Exception as e:
        logging.error(f"Hata ayıklama verileri kaydedilirken bir sorun oluştu: {e}")


# --- PlayStation Store Fiyat ve Link Alma Fonksiyonu (YENİ: Doğrudan Arama Sonucundan Veri Çekme) ---
async def get_playstation_price(game_name):
    global browser
    if not browser or not browser.is_connected():
        logging.warning("PlayStation fiyatı alınamıyor: Tarayıcı bağlı değil.")
        return None
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(90000)
        search_url = f"https://store.playstation.com/tr-tr/search/{requests.utils.quote(game_name)}"
        logging.info(f"PlayStation için gidiliyor: {search_url}")

        # Olası cookie/pop-up'ları önceden ele almak için bir kerelik bekleme
        await page.goto(search_url, wait_until='domcontentloaded')

        try:
            # Cookie banner'ını veya diğer pop-up'ları arayıp tıkla
            cookie_button = page.locator('button:has-text("Accept All Cookies"), button:has-text("Tümünü Kabul Et")')
            if await cookie_button.count() > 0:
                logging.info("Cookie banner'ı bulundu ve tıklandı.")
                await cookie_button.first.click(timeout=5000)
                # Tıkladıktan sonra sonuçların yüklenmesi için kısa bir bekleme
                await page.wait_for_timeout(2000)
        except Exception:
            logging.info("Cookie banner'ı bulunamadı veya tıklanamadı, devam ediliyor.")

        results_selector = 'div[data-qa^="search#productTile"]'
        await page.wait_for_selector(results_selector, timeout=20000)

        all_results = await page.locator(results_selector).all()
        if not all_results:
            await page.close(); return None

        # Puanlama ile en iyi eşleşmeyi bulma...
        user_query_numbers = extract_numbers_from_title(game_name)
        best_match_element = None; highest_score = -1
        for result in all_results:
            try:
                title_element = result.locator('span[data-qa$="product-name"]')
                if await title_element.count() == 0: continue
                item_name = await title_element.inner_text()
                cleaned_item_name = clean_game_name(item_name)

                base_score = 100; current_score = 0
                if cleaned_item_name.startswith(game_name): current_score = base_score - 5
                elif game_name in cleaned_item_name: current_score = base_score - 10
                else: continue
                length_penalty = len(cleaned_item_name) - len(game_name)
                current_score -= length_penalty

                result_numbers = extract_numbers_from_title(cleaned_item_name)
                if user_query_numbers:
                    if not user_query_numbers.intersection(result_numbers): current_score = -1
                else:
                    if any(n > 1 for n in result_numbers): current_score = -1

                if current_score > highest_score:
                    highest_score = current_score; best_match_element = result
            except Exception: continue

        if not best_match_element or highest_score < 50:
            await page.close(); return None

        # --- YENİ MANTIK: Veriyi doğrudan bulunan karttan çek ---
        price_info = "Fiyat bilgisi yok."
        subscriptions = []

        # Kartın içindeki metnin tamamını al
        card_text = await best_match_element.inner_text()

        # Fiyatı ara (örn: "1.399,00 TL")
        price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2}\s*TL)', card_text)
        if price_match:
            price_info = price_match.group(1)

        # Abonelikleri ara
        if "Extra" in card_text or "Premium" in card_text:
            subscriptions.append("PS Plus'a Dahil")
        if "GTA+" in card_text:
            subscriptions.append("GTA+'a Dahil")
        if "EA Play" in card_text:
            subscriptions.append("EA Play'e Dahil")

        # Link'i al
        link_element = best_match_element.locator('a.psw-link').first
        href = await link_element.get_attribute('href')
        link = "https://store.playstation.com" + href

        # Sonuçları Birleştir
        final_display_text = price_info
        if subscriptions:
            # Eğer bir abonelik varsa ama fiyat bulunamadıysa, fiyat yerine "Dahil" yazabiliriz.
            if final_display_text == "Fiyat bilgisi yok.":
                final_display_text = "Dahil"

            subscription_text = "\n*" + " & ".join(sorted(subscriptions)) + "*"
            # Eğer fiyat zaten Dahil ise, tekrar ekleme yapma
            if "Dahil" in final_display_text:
                 final_display_text = "*" + " & ".join(sorted(subscriptions)) + "*"
            else:
                 final_display_text = (final_display_text + subscription_text).strip()

        await page.close()
        return {"price": final_display_text, "link": link}

    except Exception as e:
        logging.error(f"PLAYSTATION HATA: {e}", exc_info=True)
        await take_screenshot_on_error(page, "playstation", game_name)
        if page and not page.is_closed(): await page.close()
        return None

# YENİ: Hata anında HTML ve ekran görüntüsü kaydetme
async def take_html_on_error(response, platform_name, game_name):
    if not os.path.exists('debug_output'):
        os.makedirs('debug_output')
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_game_name = re.sub(r'[^\w-]', '_', game_name)
        html_path = f"debug_output/error_{platform_name}_{safe_game_name}_{timestamp}.html"

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(response.text)
        logging.info(f"Hata anındaki sayfa HTML'i kaydedildi: {html_path}")
    except Exception as e:
        logging.error(f"Hata ayıklama HTML'i kaydedilirken sorun oluştu: {e}")

# KODDAN ÇIKARILACAK FONKSİYONLAR
# --- GÜNCELLENMİŞ: Xbox Fiyat ve Platform Fonksiyonu ---
# async def get_xbox_price_and_link_from_xbdeals(game_name):
#     ...

# --- YENİ: Xbox Link ve Platform Bilgisi (Debug Logları Eklendi) ---
# async def get_xbox_link_and_platform(game_name):
#     ...

# --- GÜNCELLENMİŞ: IsThereAnyDeal (ITAD) API Fonksiyonları ---

# BU FONKSİYONU EKLEYİN
async def get_itad_game_id(game_name):
    if not ITAD_API_KEY:
        logging.error("ITAD API anahtarı bulunamadı.")
        return None
    try:
        search_url = f"https://api.isthereanydeal.com/games/search/v1?key={ITAD_API_KEY}&title={requests.utils.quote(game_name)}"
        response = await asyncio.to_thread(requests.get, search_url)
        if response.status_code == 200:
            results = response.json()
            if results:
                # Genellikle ilk sonuç en doğrusudur.
                return results[0]['id']
        logging.warning(f"ITAD'da '{game_name}' için oyun ID'si bulunamadı. Status Code: {response.status_code}")
        return None
    except Exception as e:
        logging.error(f"ITAD OYUN ID ALMA HATA: {e}", exc_info=True)
        return None

# YENİ: Ana mağazalar dışındaki tüm CD-Key satıcılarının ID'lerini dinamik olarak alır.
async def get_itad_shop_ids():
    if not ITAD_API_KEY:
        return ""
    try:
        shops_url = f"https://api.isthereanydeal.com/service/shops/v1?key={ITAD_API_KEY}"
        response = await asyncio.to_thread(requests.get, shops_url)
        if response.status_code != 200:
            return ""

        all_shops = response.json()
        # DEĞİŞİKLİK: CD-Key sitelerini hariç tutma filtresi kaldırıldı.
        # Artık tüm mağazaları alıp, ana platformları (Steam, Epic, PS, MS) hariç tutacağız.
        excluded_shops = ["Steam", "Epic Game Store", "Playstation Store", "GOG"] # MS eklendi

        # Hariç tutulanlar dışındaki tüm mağazaların ID'lerini topla
        cdkey_shop_ids = [str(shop['id']) for shop in all_shops if shop['title'] not in excluded_shops]

        return ",".join(cdkey_shop_ids)
    except Exception as e:
        logging.error(f"ITAD Mağaza ID'leri alınırken hata: {e}")
        return "" # Hata durumunda boş string dön, böylece program çökmez

async def get_itad_subscriptions(game_id):
    if not ITAD_API_KEY or not game_id:
        return []

    try:
        url = f"https://api.isthereanydeal.com/games/subs/v1?key={ITAD_API_KEY}&country=TR"
        payload = [game_id]
        response = await asyncio.to_thread(requests.post, url, json=payload)

        if response.status_code != 200:
            logging.warning(f"ITAD abonelik bilgisi alınamadı. Status: {response.status_code}, Game ID: {game_id}")
            return []

        data = response.json()
        if not data or not data[0].get('subs'):
            return []

        subscription_names = [sub['name'] for sub in data[0]['subs']]
        logging.info(f"ITAD'dan bulunan abonelikler: {subscription_names} (Game ID: {game_id})")
        return subscription_names

    except Exception as e:
        logging.error(f"ITAD Abonelik Alma Hatası: {e}", exc_info=True)
        return []

# GÜNCELLENDİ: Artık DRM bilgisini de alıyor ve detaylı loglama yapıyor.
async def get_itad_prices(game_id, cdkey_shop_ids):
    if not ITAD_API_KEY or not game_id:
        logging.error("ITAD API anahtarı veya Oyun ID'si eksik.")
        return None

    # Microsoft Store (48), Epic Games (16) ve CD-Key mağazaları
    all_shop_ids_to_check = "48,16," + cdkey_shop_ids

    try:
        prices_url = f"https://api.isthereanydeal.com/games/prices/v3?key={ITAD_API_KEY}&country=TR&shops={all_shop_ids_to_check}"
        payload = [game_id]
        
        logging.info(f"ITAD Fiyat Sorgusu Başlatıldı. ID: {game_id}, Shops: {all_shop_ids_to_check}")
        
        response = await asyncio.to_thread(requests.post, prices_url, json=payload, timeout=20)

        if response.status_code != 200:
            logging.error(f"ITAD fiyat bilgisi alınamadı. Status: {response.status_code}, Ham Cevap: {response.text}")
            return None

        data = response.json()
                
        if not data or not data[0]:
            logging.warning(f"ITAD'dan beklenen veri gelmedi. Game ID: {game_id}")
            return None

        game_data = data[0]
        deals = game_data.get('deals', [])
        current_prices = game_data.get('current', [])

        epic_result = None
        xbox_result = None
        best_cdkey_result = None
        lowest_cdkey_price = float('inf')

        # İndirimli fiyatları işle
        for deal in deals:
            shop_id = deal.get('shop', {}).get('id')
            price_info = deal.get('price')
            link = deal.get('url')
            shop_name = deal.get('shop', {}).get('name')
            drm_list = deal.get('drm', [])
            drm_name = drm_list[0]['name'] if drm_list else None

            if not price_info or not link: continue
            
            price_amount = price_info.get('amount')
            price_currency = price_info.get('currency')
            
            formatted_price = f"{price_amount:,.2f} {price_currency}".replace(",", "X").replace(".", ",").replace("X", ".")

            if shop_id == 16 and not epic_result:
                epic_result = {"price": formatted_price, "link": link, "shop": shop_name}
            elif shop_id == 48 and not xbox_result: # ID 48 kullanılıyor
                xbox_result = {"price": formatted_price, "link": link, "shop": shop_name}
                logging.info(f"✅ ITAD Xbox Fiyatı (Deals) Bulundu: {formatted_price} - {link}")
            else:
                if price_amount < lowest_cdkey_price:
                    lowest_cdkey_price = price_amount
                    best_cdkey_result = {"price": formatted_price, "link": link, "shop": shop_name, "drm": drm_name}

        # İndirimli fiyat bulunamadıysa, güncel fiyatları kontrol et
        if not epic_result or not xbox_result:
            for current in current_prices:
                shop_id = current.get('shop', {}).get('id')
                price_info = current.get('price')
                link = current.get('url')
                shop_name = current.get('shop', {}).get('name')
                
                if not price_info or not link: continue

                price_amount = price_info.get('amount')
                price_currency = price_info.get('currency')
                
                formatted_price = f"{price_amount:,.2f} {price_currency}".replace(",", "X").replace(".", ",").replace("X", ".")

                if shop_id == 16 and not epic_result:
                    epic_result = {"price": formatted_price, "link": link, "shop": shop_name}
                elif shop_id == 48 and not xbox_result: # ID 48 kullanılıyor
                    xbox_result = {"price": formatted_price, "link": link, "shop": shop_name}
                    logging.info(f"✅ ITAD Xbox Fiyatı (Current) Bulundu: {formatted_price} - {link}")
        
        if not xbox_result:
            logging.warning(f"ITAD'da Microsoft Store (ID 48) için ne indirimli ne de güncel fiyat bulunamadı. Oyun ID: {game_id}")
        
        if not epic_result:
            logging.warning(f"ITAD'da Epic Games Store (ID 16) için ne indirimli ne de güncel fiyat bulunamadı. Oyun ID: {game_id}")

        return {"epic": epic_result, "xbox": xbox_result, "cdkey": best_cdkey_result}

    except Exception as e:
        logging.error(f"ITAD FİYAT ALMA KRİTİK HATA: {e}", exc_info=True)
        return None

# YENİ: Belirtilen mağazalar için tarihi en düşük fiyatları alır.
async def get_historical_lows(game_id):
    if not ITAD_API_KEY or not game_id:
        return {}

    # ITAD Mağaza ID'leri: Steam (61), Epic Games (16), Microsoft Store (15)
    shop_ids_for_lows = "61,16,48"

    try:
        url = f"https://api.isthereanydeal.com/games/storelow/v2?key={ITAD_API_KEY}&country=TR&shops={shop_ids_for_lows}"
        payload = [game_id]
        response = await asyncio.to_thread(requests.post, url, json=payload)

        if response.status_code != 200:
            return {}

        data = response.json()
        if not data or not data[0].get('lows'):
            return {}

        # Sonuçları mağaza ID'sine göre map'leyelim
        historical_lows = {}
        for low in data[0]['lows']:
            shop_id = low.get('shop', {}).get('id')
            price_info = low.get('price')
            if shop_id and price_info:
                price_amount = price_info.get('amount')
                price_currency = price_info.get('currency')
                formatted_price = f"{price_amount:,.2f} {price_currency}".replace(",", "X").replace(".", ",").replace("X", ".")
                historical_lows[shop_id] = formatted_price

        return historical_lows

    except Exception as e:
        logging.error(f"ITAD Tarihi Düşük Fiyat Alma Hatası: {e}", exc_info=True)
        return {}

# --- Discord Bot Ana Kodları ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    global playwright, browser
    logging.info(f'{client.user} olarak Discord\'a giriş yapıldı.')
    try:
        playwright = await async_playwright().start()
        # headless=False yaparak tarayıcıyı Replit'te VNC ile görebilirsiniz (debug için faydalı olabilir)
        browser = await playwright.chromium.launch(headless=True)
        logging.info("✅ Tarayıcı (PS & Xbox için) başarıyla başlatıldı!")
    except Exception as e:
        logging.error(f"❌ HATA: Playwright tarayıcısı başlatılamadı: {e}", exc_info=True)

# --- GÜNCELLENMİŞ: on_message Fonksiyonu ---
@client.event
async def on_message(message):
    if message.author == client.user: 
        return

    if message.content.lower().startswith('!fiyat '):
        oyun_adi_orjinal = message.content[7:].strip()
        if not oyun_adi_orjinal: 
            await message.channel.send("Lütfen bir oyun adı girin.")
            return

        oyun_adi_temiz = clean_game_name(oyun_adi_orjinal)
        msg = await message.channel.send(f"**{oyun_adi_orjinal}** için mağazalar kontrol ediliyor...")
        logging.info(f"Fiyat sorgusu başlatıldı: '{oyun_adi_orjinal}' (Temizlenmiş: '{oyun_adi_temiz}')")

        steam_sonucu = await asyncio.to_thread(get_steam_price, oyun_adi_temiz)

        display_game_name = oyun_adi_orjinal
        search_name_for_itad = oyun_adi_temiz
        if isinstance(steam_sonucu, dict) and steam_sonucu.get("name"):
            display_game_name = steam_sonucu['name']
            search_name_for_itad = clean_game_name(steam_sonucu['name'])

        itad_game_id_task = get_itad_game_id(search_name_for_itad)
        cdkey_shop_ids_task = get_itad_shop_ids()
        itad_game_id, cdkey_shop_ids = await asyncio.gather(itad_game_id_task, cdkey_shop_ids_task)

        tasks = {
            "ps": get_playstation_price(oyun_adi_temiz),
            "itad_cdkey_prices": get_itad_prices(itad_game_id, cdkey_shop_ids),
            "historical_lows": get_historical_lows(itad_game_id),
            "itad_subscriptions": get_itad_subscriptions(itad_game_id),
        }
        # Xbox'u ayrı görev olarak almamıza gerek kalmadı.

        results_from_gather = await asyncio.gather(*tasks.values(), return_exceptions=True)
        sonuclar = dict(zip(tasks.keys(), results_from_gather))
        sonuclar["steam"] = steam_sonucu

        itad_all_prices = sonuclar.pop("itad_cdkey_prices", None)
        if isinstance(itad_all_prices, dict):
            sonuclar["epic"] = itad_all_prices.get("epic")
            sonuclar["cdkey"] = itad_all_prices.get("cdkey")
            # DEĞİŞİKLİK: Xbox sonucunu da ITAD'dan çek.
            sonuclar["xbox"] = itad_all_prices.get("xbox")


        subscriptions = sonuclar.get("itad_subscriptions", [])
        historical_lows = sonuclar.get("historical_lows", {})

        # Embed oluştur
        embed = discord.Embed(title=f"🎮 {display_game_name} Fiyat Bilgisi ve Linkler V.0.95", color=discord.Color.from_rgb(16, 124, 16))
        embed.set_footer(text="Fiyatlar anlık olarak mağazalardan ve bazı API'lerden çekilmektedir.")

        # Fonksiyonlar bulunamadığında dönecek metinleri tanımlayalım.
        # DEĞİŞİKLİK: Daha açıklayıcı metinler.
        def get_not_found_text(platform_name):
            return f"*Mağazada bulunamadı ya da satışta değil.*"

        # --- Steam için alan ekle ---
        steam_result = sonuclar.get("steam")
        steam_price_info = steam_result.get("price", "N/A") if steam_result else "N/A"
        steam_link = steam_result.get("link", "#") if steam_result else "#"

        display_text_steam = "N/A"
        if isinstance(steam_price_info, tuple):
            price, currency = steam_price_info
            try_rate = get_usd_to_try_rate()
            if try_rate and currency == "USD":
                tl_price = price * try_rate
                display_text_steam = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)".replace(",", "X").replace(".", ",").replace("X", ".")
            else:
                display_text_steam = f"{price} {currency}"
        elif steam_price_info != "N/A":
            display_text_steam = str(steam_price_info)

        if display_text_steam == "N/A":
             field_value_steam = get_not_found_text("Steam")
        else:
            low_price_steam = historical_lows.get(61)
            if low_price_steam:
                display_text_steam += f"\n*En Düşük Fiyat: {low_price_steam}*"
            field_value_steam = f"[{display_text_steam}]({steam_link})"
        embed.add_field(name="Steam", value=field_value_steam, inline=True)

        # --- Xbox için alan ekle (ITAD'dan çekilen) ---
        # DEĞİŞİKLİK: Xbox fiyatı artık ITAD'dan geliyor.
        xbox_result = sonuclar.get("xbox")
        xbox_price = xbox_result.get("price") if xbox_result else None
        xbox_link = xbox_result.get("link", "#") if xbox_result else "#"

        display_text_xbox = None # Başlangıç değeri None

        if xbox_price:
            display_text_xbox = xbox_price
        
        subs_to_show = []
        if any("Game Pass" in s for s in subscriptions): subs_to_show.append("Game Pass'e Dahil")
        if any("EA Play" in s for s in subscriptions): subs_to_show.append("EA Play'e Dahil")
        
        if display_text_xbox:
            # Fiyat bulunduysa, altına abonelik bilgisini ekle
            if subs_to_show:
                display_text_xbox += "\n*" + " & ".join(subs_to_show) + "*"
            low_price_xbox = historical_lows.get(15)
            if low_price_xbox:
                display_text_xbox += f"\n*En Düşük Fiyat: {low_price_xbox}*"
            field_value_xbox = f"[{display_text_xbox}]({xbox_link})"
        elif subs_to_show:
            # Fiyat bulunamadı ANCAK abonelik varsa
            display_text_xbox = "Fiyat Bulunamadı." # Fiyat N/A olduğu için bunu göster.
            display_text_xbox += "\n*" + " & ".join(subs_to_show) + "*"
            # Bu durumda linki de Game Pass arama linkine yönlendirebiliriz, ancak şimdilik '#' bırakalım.
            field_value_xbox = f"[{display_text_xbox}]({xbox_link})"
        else:
            # Hem fiyat hem abonelik yoksa
            field_value_xbox = get_not_found_text("Xbox")
            
        embed.add_field(name="Xbox", value=field_value_xbox, inline=True)

        # --- PlayStation için alan ekle ---
        ps_result = sonuclar.get("ps")
        ps_price = ps_result.get("price") if ps_result and ps_result.get("price") else None
        ps_link = ps_result.get("link", "#") if ps_result else "#"

        if ps_price:
            field_value_ps = f"[{ps_price}]({ps_link})"
        else:
            field_value_ps = get_not_found_text("PlayStation")
        embed.add_field(name="PlayStation", value=field_value_ps, inline=True)

        # --- Epic Games için alan ekle ---
        epic_result = sonuclar.get("epic")
        epic_price = epic_result.get("price") if epic_result else None
        epic_link = epic_result.get("link", "#") if epic_result else "#"

        if epic_price:
            display_text_epic = epic_price
            low_price_epic = historical_lows.get(16)
            if low_price_epic:
                display_text_epic += f"\n*En Düşük Fiyat: {low_price_epic}*"
            field_value_epic = f"[{display_text_epic}]({epic_link})"
        else:
            field_value_epic = get_not_found_text("Epic Games")
        embed.add_field(name="Epic Games", value=field_value_epic, inline=True)

        # --- CD-Key için alan ekle ---
        cdkey_result = sonuclar.get("cdkey")
        cdkey_price = cdkey_result.get("price") if cdkey_result else None
        cdkey_link = cdkey_result.get("link", "#") if cdkey_result else "#"
        cdkey_drm = cdkey_result.get("drm") if cdkey_result else None

        if cdkey_price:
            display_text_cdkey = cdkey_price
            if cdkey_drm:
                display_text_cdkey += f" - {cdkey_drm}"
            field_value_cdkey = f"[{display_text_cdkey}]({cdkey_link})"
        else:
            field_value_cdkey = "*CD-Key mağazalarında indirimli bulunamadı.*"
        embed.add_field(name="En Ucuz CD-Key", value=field_value_cdkey, inline=True)

        await msg.edit(content=None, embed=embed)

# --- Botu ve Sunucuyu Başlatma ---
# keep_alive() # Gerekliyse yorum satırını kaldırın
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if DISCORD_TOKEN:
    client.run(DISCORD_TOKEN)
else:
    logging.critical("HATA: DISCORD_TOKEN .env dosyasında bulunamadı.")