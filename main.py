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

# --- YENİ: Debug ve Hata Ayıklama Kurulumu ---
# Replit'te çalışırken logları ve ekran görüntülerini saklamak için bir klasör oluşturalım.
if not os.path.exists('debug_output'):
    os.makedirs('debug_output')

# Loglama yapılandırması: Hem dosyaya hem de konsola log basacak.
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

# --- YENİ: Hata durumunda ekran görüntüsü VE HTML KAYDEDEN yardımcı fonksiyon ---
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

# --- Xbox Store Fiyat ve Link Alma Fonksiyonu (JSON VERİSİ OKUYAN FİNAL VERSİYON) ---
async def get_xbox_price(game_name_clean):
    global browser
    if not browser or not browser.is_connected():
        logging.warning("Xbox fiyatı alınamıyor: Tarayıcı bağlı değil.")
        return None
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(10000)
        search_url = f"https://www.xbox.com/tr-TR/Search/Results?q={requests.utils.quote(game_name_clean)}"
        logging.info(f"Xbox için gidiliyor: {search_url}")
        
        await page.goto(search_url)
        await page.wait_for_selector('div[class*="ProductCard-module"]')

        # ... (En iyi eşleşmeyi bulma mantığı aynı) ...
        all_results = await page.query_selector_all('a[class*="commonStyles-module__basicButton"]')
        if not all_results:
            await page.close(); return None
        user_query_numbers = extract_numbers_from_title(game_name_clean)
        best_match_element = None; highest_score = -1
        for result in all_results:
            full_aria_label = await result.get_attribute("aria-label") or ""
            if not full_aria_label: continue
            item_name = full_aria_label.split(',')[0].strip()
            cleaned_item_name = clean_game_name(item_name)
            current_score = 0
            if game_name_clean in cleaned_item_name: current_score += 90
            elif cleaned_item_name in game_name_clean: current_score += 85
            else: continue
            result_numbers = extract_numbers_from_title(cleaned_item_name)
            if user_query_numbers:
                if not user_query_numbers.intersection(result_numbers): current_score -= 100
            else:
                if any(n > 1 for n in result_numbers): current_score -= 100
            if current_score > highest_score:
                highest_score = current_score; best_match_element = result
        if not best_match_element or highest_score < 50:
            await page.close(); return None

        await best_match_element.click()
        await page.wait_for_load_state('domcontentloaded', timeout=60000)
        link = page.url

        price_info = "Fiyat bilgisi yok."
        subscriptions = []
        platform_info = None

        # --- YENİ VE KESİN ABONELİK TESPİTİ (JSON'DAN OKUMA) ---
        try:
            # Sayfanın URL'sinden ürün ID'sini al (örn: 9NWQ4TJKPJ7B)
            product_id_match = re.search(r'/([A-Z0-9]{12})', link)
            if product_id_match:
                product_id = product_id_match.group(1)
                logging.info(f"Xbox Ürün ID'si bulundu: {product_id}")

                # Sayfanın içine gömülü olan veri script'ini çek
                script_selector = 'script:has-text("__PRELOADED_STATE__")'
                script_content = await page.locator(script_selector).inner_text()
                
                # Script içeriğini temizleyip JSON'a çevir
                json_str = script_content.replace("window.__PRELOADED_STATE__ = ", "").rstrip(";")
                preloaded_data = json.loads(json_str)

                # JSON verisi içinde ürünün abonelik bilgilerini kontrol et
                product_summary = preloaded_data.get("core2", {}).get("products", {}).get("productSummaries", {}).get(product_id, {})
                
                if product_summary:
                    # Bu liste doluysa, oyun en az bir aboneliğe dahildir.
                    included_passes = product_summary.get("includedWithPassesProductIds", [])
                    if included_passes:
                        # Hangi abonelik olduğunu da bulabiliriz ama şimdilik dahil olması yeterli.
                        # EA Play ID: CFQ7TTC0K5DH, Game Pass ID'leri: CFQ7TTC0KHS0, CFQ7TTC0KGQ8...
                        is_ea_play = "CFQ7TTC0K5DH" in included_passes
                        is_game_pass = any(p != "CFQ7TTC0K5DH" for p in included_passes)

                        if is_game_pass:
                            subscriptions.append("Game Pass'e Dahil")
                        if is_ea_play:
                            subscriptions.append("EA Play'e Dahil")
                        logging.info(f"JSON verisinden abonelikler bulundu: {subscriptions}")

        except Exception as e:
            logging.error(f"Xbox JSON abonelik verisi okunurken hata (Görsel arama denenecek): {e}")
        
        # Fiyat ve Platform tespiti (Bu kısımlar zaten sağlam, aynı kalıyor)
        try:
            price_selector_A = 'span[class*="Price-module__boldText"]'
            price_element_A = page.locator(price_selector_A).first
            await price_element_A.wait_for(state="visible", timeout=7000)
            price_info = await price_element_A.inner_text()
        except Exception:
            try:
                price_selector_B = 'button[aria-label*="satın al"] span[class*="Price-module__boldText"]'
                price_element_B = page.locator(price_selector_B).first
                await price_element_B.wait_for(state="visible", timeout=7000)
                price_info = await price_element_B.inner_text()
            except Exception:
                try:
                    button_selector_C = 'button[aria-label*="fiyatı"]'
                    button_element_C = page.locator(button_selector_C).first
                    await button_element_C.wait_for(state="visible", timeout=7000)
                    aria_label = await button_element_C.get_attribute("aria-label")
                    price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2}\s*₺)', aria_label)
                    if price_match: price_info = price_match.group(1)
                except Exception as e:
                    logging.error(f"Xbox fiyatı 3 yöntemle de bulunamadı: {e}")
                    await take_screenshot_on_error(page, "xbox_price_error", game_name_clean)
        try:
            platform_list_locator = page.locator('h2:has-text("Platformlar") + ul')
            if await platform_list_locator.count() > 0:
                all_platforms_text = await platform_list_locator.first.inner_text()
                has_pc = "Bilgisayar" in all_platforms_text
                has_xbox = "Xbox" in all_platforms_text
                if has_pc and has_xbox: platform_info = "PC & Konsol"
                elif has_xbox: platform_info = "Konsol"
        except Exception:
            logging.info("Xbox platform bilgisi alınamadı.")
        
        await page.close()

        # Sonuçları birleştir
        display_lines = []
        first_line_parts = []
        if not (price_info == "Fiyat bilgisi yok." and subscriptions):
            first_line_parts.append(price_info)
        if platform_info:
            first_line_parts.append(f"({platform_info})")
        if first_line_parts:
            display_lines.append(" ".join(first_line_parts))
        if subscriptions:
            display_lines.append("*" + " veya ".join(subscriptions) + "*")
        final_display_text = "\n".join(display_lines)

        return {"price": final_display_text.strip(), "link": link}

    except Exception as e:
        logging.error(f"XBOX HATA (Genel Fonksiyon Hatası): {e}", exc_info=True)
        await take_screenshot_on_error(page, "xbox_general_error", game_name_clean)
        if page and not page.is_closed(): await page.close()
        return None

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
        # Ana platformları ve büyük mağazaları hariç tutalım
        excluded_shops = ["Steam", "Epic Game Store", "Microsoft Store", "Playstation Store", "GOG"]
        
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

# GÜNCELLENDİ: Artık DRM bilgisini de alıyor.
async def get_itad_prices(game_id, cdkey_shop_ids):
    if not ITAD_API_KEY or not game_id:
        return None

    all_shop_ids_to_check = "16," + cdkey_shop_ids

    try:
        prices_url = f"https://api.isthereanydeal.com/games/prices/v3?key={ITAD_API_KEY}&country=TR&shops={all_shop_ids_to_check}"
        payload = [game_id]
        response = await asyncio.to_thread(requests.post, prices_url, json=payload)

        if response.status_code != 200:
            logging.warning(f"ITAD fiyat bilgisi alınamadı. Status: {response.status_code}, Game ID: {game_id}")
            return None

        data = response.json()
        if not data or not data[0].get('deals'):
            logging.info(f"ITAD'da bu mağazalar için aktif bir indirim bulunamadı. Game ID: {game_id}")
            return None

        deals = data[0]['deals']
        epic_result = None
        best_cdkey_result = None
        lowest_cdkey_price = float('inf')

        for deal in deals:
            shop_id = deal.get('shop', {}).get('id')
            price_info = deal.get('price')
            link = deal.get('url')
            shop_name = deal.get('shop', {}).get('name')
            
            # --- DEĞİŞİKLİK 1: DRM bilgisini çek ---
            drm_list = deal.get('drm', [])
            # Genellikle ilk DRM doğru platformdur (örn: "Steam")
            drm_name = drm_list[0]['name'] if drm_list else None

            if not price_info or not link:
                continue
            
            price_amount = price_info.get('amount')
            price_currency = price_info.get('currency')

            if shop_id == 16: # Epic Games Store ID'si
                epic_result = {
                    "price": f"{price_amount:,.2f} {price_currency}".replace(",", "X").replace(".", ",").replace("X", "."),
                    "link": link,
                    "shop": shop_name
                }
            else:
                if price_amount < lowest_cdkey_price:
                    lowest_cdkey_price = price_amount
                    best_cdkey_result = {
                        "price": f"{price_amount:,.2f} {price_currency}".replace(",", "X").replace(".", ",").replace("X", "."),
                        "link": link,
                        "shop": shop_name,
                        "drm": drm_name  # --- DEĞİŞİKLİK 2: DRM'i sonuca ekle ---
                    }
        
        return {"epic": epic_result, "cdkey": best_cdkey_result}

    except Exception as e:
        logging.error(f"ITAD FİYAT ALMA HATA: {e}", exc_info=True)
        return None

# YENİ: Belirtilen mağazalar için tarihi en düşük fiyatları alır.
async def get_historical_lows(game_id):
    if not ITAD_API_KEY or not game_id:
        return {}
        
    # ITAD Mağaza ID'leri: Steam (61), Epic Games (16), Microsoft Store (15)
    shop_ids_for_lows = "61,16,15"
    
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

@client.event
async def on_message(message):
    if message.author == client.user: return
    if message.content.lower().startswith('!fiyat '):
        # ... (fonksiyonun başındaki tüm kodlar aynı kalacak) ...
        oyun_adi_orjinal = message.content[7:].strip()
        if not oyun_adi_orjinal: await message.channel.send("Lütfen bir oyun adı girin."); return
        oyun_adi_temiz = clean_game_name(oyun_adi_orjinal)
        msg = await message.channel.send(f"**{oyun_adi_orjinal}** için mağazalar kontrol ediliyor...")
        logging.info(f"Fiyat sorgusu başlatıldı: '{oyun_adi_orjinal}' (Temizlenmiş: '{oyun_adi_temiz}')")

        steam_sonucu = await asyncio.to_thread(get_steam_price, oyun_adi_temiz)
        sonuclar = {"steam": steam_sonucu}
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
            "xbox": get_xbox_price(oyun_adi_temiz),
            "itad_cdkey_prices": get_itad_prices(itad_game_id, cdkey_shop_ids),
            "historical_lows": get_historical_lows(itad_game_id),
            "itad_subscriptions": get_itad_subscriptions(itad_game_id),
        }
        
        results_from_gather = await asyncio.gather(*tasks.values(), return_exceptions=True)
        sonuclar.update(dict(zip(tasks.keys(), results_from_gather)))

        itad_cdkey_results = sonuclar.pop("itad_cdkey_prices", None)
        if isinstance(itad_cdkey_results, dict):
            sonuclar["epic"] = itad_cdkey_results.get("epic")
            sonuclar["cdkey"] = itad_cdkey_results.get("cdkey")
        
        subscriptions = sonuclar.get("itad_subscriptions", [])
        historical_lows = sonuclar.get("historical_lows", {})

        embed = discord.Embed(title=f"🎮 {display_game_name} Fiyat Bilgisi ve Linkler V.0.85", color=discord.Color.from_rgb(16, 124, 16))
        embed.set_footer(text="Fiyatlar anlık olarak mağazalardan ve IsThereAnyDeal API'sinden çekilmektedir.")

        store_order = ["steam", "xbox", "ps", "epic", "cdkey"]

        for store in store_order:
            result = sonuclar.get(store)
            store_name_map = {"steam": "Steam", "cdkey": "En Ucuz CD-Key", "ps": "PlayStation", "xbox": "Xbox", "epic": "Epic Games"}
            store_name = store_name_map[store]

            if isinstance(result, Exception):
                embed.add_field(name=store_name, value=f"`Hata oluştu.`", inline=True)
            elif result is None:
                embed.add_field(name=store_name, value="`Bulunamadı.`", inline=True)
            else:
                price_info = result.get("price", "N/A")
                link = result.get("link", "#")
                
                if store == "cdkey" and result.get("shop"):
                    store_name = f"En Ucuz CD-Key ({result.get('shop')})"

                display_text = ""
                if store == "steam" and isinstance(price_info, tuple):
                    price, currency = price_info
                    try_rate = get_usd_to_try_rate()
                    if try_rate and currency == "USD":
                        tl_price = price * try_rate
                        display_text = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)".replace(",", "X").replace(".", ",").replace("X", ".")
                    else:
                        display_text = f"{price} {currency}"
                else:
                    display_text = str(price_info)

                # --- DEĞİŞİKLİK: CD-Key için DRM bilgisini formatla ---
                if store == 'cdkey':
                    drm = result.get("drm")
                    if drm:
                        display_text += f" - {drm}"
                
                if store == 'xbox' and subscriptions:
                    subs_to_show = []
                    if any("Game Pass" in s for s in subscriptions):
                        subs_to_show.append("Game Pass'e Dahil")
                    if any("EA Play" in s for s in subscriptions):
                        subs_to_show.append("EA Play'e Dahil")
                    
                    if subs_to_show:
                        display_text += "\n*" + " veya ".join(subs_to_show) + "*"

                low_price = None
                if store == 'steam' and 61 in historical_lows: low_price = historical_lows[61]
                elif store == 'epic' and 16 in historical_lows: low_price = historical_lows[16]
                elif store == 'xbox' and 15 in historical_lows: low_price = historical_lows[15]
                
                if low_price:
                    # Tarihi düşük fiyat etiketini "En Düşük Fiyat" olarak değiştirelim
                    display_text += f"\n*En Düşük Fiyat: {low_price}*"
                
                embed.add_field(name=store_name, value=f"[{display_text}]({link})", inline=True)

        await msg.edit(content=None, embed=embed)

# --- Botu ve Sunucuyu Başlatma ---
# keep_alive() # Gerekliyse yorum satırını kaldırın
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if DISCORD_TOKEN:
    client.run(DISCORD_TOKEN)
else:
    logging.critical("HATA: DISCORD_TOKEN .env dosyasında bulunamadı.")