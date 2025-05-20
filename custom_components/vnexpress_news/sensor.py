import feedparser
import aiohttp
from bs4 import BeautifulSoup
import json
import time
import google.generativeai as genai
import configparser
import os
from datetime import datetime, timedelta
from google.api_core import exceptions
import logging
import re
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from . import DOMAIN
import asyncio
import functools

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=600)  # 10 phút
RSS_URL = "https://vnexpress.net/rss/tin-moi-nhat.rss"
MAX_TITLES = 200
DEFAULT_NAME = "VNExpress News"

async def load_config(hass, file_path="config.txt"):
    """Đọc cấu hình từ file config.txt bất đồng bộ."""
    _LOGGER.debug("Đọc file config.txt")
    config_path = hass.config.path(file_path)
    _LOGGER.debug(f"Đường dẫn: {config_path}")

    def read_config_sync():
        if not os.path.exists(config_path):
            _LOGGER.error(f"File config.txt không tồn tại tại {config_path}")
            raise FileNotFoundError(f"File config.txt không tồn tại")
        config = configparser.ConfigParser()
        config.read(config_path)
        try:
            cfg = {'GEMINI_API_KEY': config['DEFAULT']['GEMINI_API_KEY']}
            _LOGGER.debug("Đọc cấu hình thành công")
            return cfg
        except KeyError as e:
            _LOGGER.error(f"Lỗi: Thiếu khóa {e} trong config.txt")
            raise

    return await hass.loop.run_in_executor(None, read_config_sync)

async def load_titles(hass, file_path="titles.txt"):
    """Đọc dữ liệu từ file titles.txt bất đồng bộ."""
    _LOGGER.debug("Đọc file titles.txt")
    titles_data = []
    file_path = hass.config.path(file_path)
    _LOGGER.debug(f"Đường dẫn: {file_path}")

    def read_titles_sync():
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            data = json.loads(line.strip())
                            data['title'] = data['title'].encode('utf-8').decode('utf-8')
                            data['content'] = data['content'].encode('utf-8').decode('utf-8')
                            data['summary'] = data['summary'].encode('utf-8').decode('utf-8')
                            titles_data.append(data)
                            _LOGGER.debug(f"Đã đọc: {data['title']}")
                        except json.JSONDecodeError as e:
                            _LOGGER.error(f"Lỗi parse dòng: {e}")
                            continue
        else:
            _LOGGER.info("titles.txt chưa tồn tại, sẽ tạo mới")
        _LOGGER.debug(f"Đã đọc {len(titles_data)} bản ghi")
        return titles_data

    return await hass.loop.run_in_executor(None, read_titles_sync)

async def save_titles(hass, titles_data, file_path="titles.txt"):
    """Lưu dữ liệu vào file titles.txt bất đồng bộ, sắp xếp mới nhất trước."""
    _LOGGER.debug("Lưu vào titles.txt")
    file_path = hass.config.path(file_path)

    def write_titles_sync():
        unique_titles = {item['title']: item for item in titles_data}
        sorted_titles = sorted(unique_titles.values(), key=lambda x: datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S'), reverse=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            for data in sorted_titles:
                f.write(json.dumps(data, ensure_ascii=False) + '\n')
                _LOGGER.debug(f"Đã lưu: {data['title']}")
        _LOGGER.debug(f"Đã lưu {len(sorted_titles)} bản ghi")

    await hass.loop.run_in_executor(None, write_titles_sync)

def model1(api_key, model_name='gemini-2.0-flash-lite'):
    _LOGGER.debug(f"Khởi tạo model: {model_name}")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)

def model2(api_key, model_name='gemini-2.0-flash'):
    _LOGGER.debug(f"Khởi tạo model dự phòng: {model_name}")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)

def count_words(text):
    return len(text.split())

async def summarize_content(model, content, api_key, max_length=50, delay=5, retry_delay=5, max_retries=5):
    _LOGGER.debug(f"Tóm tắt nội dung, dài: {count_words(content)} từ")
    await asyncio.sleep(delay)

    if count_words(content) < 50:
        _LOGGER.debug("Nội dung ngắn, trả nguyên văn")
        return content

    retries = 0
    current_model = model
    using_lite_model = False

    while retries < max_retries:
        try:
            _LOGGER.info(f"[Gemini] Model: {current_model._model_name}")
            prompt = f"Tóm tắt nội dung sau thành tối đa {max_length} từ bằng tiếng Việt:\n\n{content}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, functools.partial(current_model.generate_content, prompt)
            )
            _LOGGER.info("Tóm tắt thành công")
            return response.text.strip()
        except exceptions.ResourceExhausted as e:
            retries += 1
            _LOGGER.error(f"[Gemini] Lỗi quota (429): {e}. Thử lại {retries}/{max_retries}")
            if retries >= max_retries and not using_lite_model:
                _LOGGER.info("Chuyển model dự phòng (gemini-2.0-flash-lite)")
                current_model = model2(api_key)
                using_lite_model = True
                retries = 0
            elif retries >= max_retries and using_lite_model:
                _LOGGER.error("Hết lượt thử cả hai model")
                return f"Lỗi quota: {str(e)}"
            await asyncio.sleep(retry_delay)
        except Exception as e:
            _LOGGER.error(f"Lỗi: {e}")
            return f"Lỗi tóm tắt: {str(e)}"
    _LOGGER.error("Hết số lần thử")
    return f"Lỗi: Hết số lần thử ({max_retries})"

async def fetch_full_article(url, published_time=None, session=None):
    _LOGGER.debug(f"Lấy bài báo: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with session.get(url, headers=headers, timeout=10) as response:
            response.raise_for_status()
            text = await response.text()
            soup = BeautifulSoup(text, 'html.parser')

            title = (
                soup.find('h1', class_='title-detail') or
                soup.find('h1', class_='title-news') or
                soup.find('h1', class_='title-page detail') or
                soup.find('title')  # fallback cuối cùng
            )
            title_text = title.get_text(strip=True) if title else 'Không tìm thấy tiêu đề'

            content = (
                soup.find('article', class_='fck_detail') or
                soup.find('div', class_='podcast-content')
            )    
            content_text = '\n'.join(p.get_text(strip=True) for p in content.find_all('p') if p.get_text(strip=True)) if content else 'Không tìm thấy nội dung'
            if "Liên hệ:" in content_text:
                content_text = content_text.split("Liên hệ:")[0].strip()
            article_time = published_time if published_time else time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())

            _LOGGER.debug(f"Lấy thành công: {title_text}")
            return {
                'title': title_text,
                'time': article_time,
                'content': content_text,
                'link': url
            }
    except Exception as e:
        _LOGGER.error(f"Lỗi lấy bài báo: {e}")
        return {
            'title': 'Lỗi',
            'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
            'content': f"Không thể lấy nội dung: {str(e)}",
            'link': url
        }

async def fetch_rss_news(hass, gemini_model, titles_data, api_key, num_articles=60):
    _LOGGER.debug("Lấy tin từ RSS")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RSS_URL, timeout=10) as response:
                response.raise_for_status()
                rss_content = await response.text()

        def parse_rss_sync():
            return feedparser.parse(rss_content)

        feed = await hass.loop.run_in_executor(None, parse_rss_sync)
        articles = feed.entries
        news_list = []
        existing_titles = {item['title'] for item in titles_data}
        updated_titles_data = titles_data.copy()
        has_new_article = False

        async with aiohttp.ClientSession() as session:
            for article in articles:
                link = article.get('link', '')
                published_time = article.get('published', None)
                if published_time:
                    try:
                        published_time = datetime.strptime(published_time, '%a, %d %b %Y %H:%M:%S %z').strftime('%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        published_time = None
                
                title = article.get('title', 'Không tìm thấy tiêu đề')
                _LOGGER.debug(f"Kiểm tra bài: {title}")
                
                if title not in existing_titles:
                    full_article = await fetch_full_article(link, published_time, session)
                    if full_article['title'] != 'Lỗi':
                        has_new_article = True
                        summary = await summarize_content(gemini_model, full_article['content'], api_key) if full_article['content'] else 'Không có nội dung'
                        _LOGGER.info(f"Tóm tắt thành công: {full_article['title']}")
                        new_entry = {
                            'title': full_article['title'],
                            'time': full_article['time'],
                            'content': full_article['content'],
                            'summary': summary,
                            'is_new': True
                        }
                        updated_titles_data.append(new_entry)
                        news_list.append(new_entry)
                else:
                    for existing_article in titles_data:
                        if existing_article['title'] == title:
                            existing_article['is_new'] = False
                            news_list.append(existing_article)
                            _LOGGER.debug(f"Bài đã tồn tại: {title}")
                            break

                if len(news_list) >= num_articles:
                    break

        if len(news_list) < num_articles:
            remaining = num_articles - len(news_list)
            _LOGGER.debug(f"Thêm {remaining} bài từ titles.txt")
            sorted_titles_data = sorted(titles_data, key=lambda x: datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S'), reverse=True)
            for item in sorted_titles_data:
                if len(news_list) >= num_articles:
                    break
                if item['title'] not in {news['title'] for news in news_list}:
                    item['is_new'] = False
                    news_list.append(item)
                    _LOGGER.debug(f"Thêm bài cũ: {item['title']}")

        if len(updated_titles_data) > MAX_TITLES:
            _LOGGER.debug(f"Giới hạn xuống {MAX_TITLES} bản ghi")
            updated_titles_data = sorted(updated_titles_data, key=lambda x: datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S'), reverse=True)
            updated_titles_data = updated_titles_data[:MAX_TITLES]

        await save_titles(hass, updated_titles_data)

        news_list = sorted(news_list, key=lambda x: (
            0 if x.get('is_new', False) else 1,
            -datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S').timestamp()
        ))

        news_dict = {}
        for i, news in enumerate(news_list[:num_articles], 1):
            padded_index = f"{i:02d}"
            key = f"Tin {padded_index} (Tin mới)" if news.get('is_new', False) else f"Tin {padded_index}"
            news_dict[key] = f"Tiêu Đề: {news['title']}\nNội Dung: {news['summary']}"
            _LOGGER.debug(f"Thêm tin: {key}")

        _LOGGER.debug(f"Hoàn thành, có {len(news_dict)} tin, has_new_article: {has_new_article}")
        return news_dict, has_new_article
    except Exception as e:
        _LOGGER.error(f"Lỗi lấy tin RSS: {e}")
        raise

async def async_setup_platform(hass: HomeAssistant, config: ConfigType, async_add_entities: AddEntitiesCallback, discovery_info: DiscoveryInfoType | None = None) -> None:
    """Thiết lập sensor VNExpress News."""
    _LOGGER.info("Thiết lập sensor VNExpress News")
    try:
        _LOGGER.debug("Tải config.txt")
        config_data = await load_config(hass)
        _LOGGER.debug("Khởi tạo Gemini model")
        gemini_model = model1(config_data['GEMINI_API_KEY'])
        _LOGGER.debug("Tải titles.txt")
        titles_data = await load_titles(hass)
        _LOGGER.debug(f"Đã tải {len(titles_data)} bản ghi")
        sensor = VNExpressNewsSensor(hass, config_data['GEMINI_API_KEY'], gemini_model, titles_data)
        async_add_entities([sensor])
        await sensor.async_update()
        _LOGGER.info("Thiết lập sensor thành công")
    except Exception as e:
        _LOGGER.error(f"Lỗi thiết lập sensor: {e}", exc_info=True)
        raise


class VNExpressNewsSensor(SensorEntity, RestoreEntity):
    """Sensor VNExpress News."""

    def __init__(self, hass, api_key, gemini_model, titles_data):
        """Khởi tạo sensor."""
        _LOGGER.debug("Khởi tạo VNExpressNewsSensor")
        self.hass = hass
        self._api_key = api_key
        self._gemini_model = gemini_model
        self._titles_data = titles_data
        self._state = "Không có tin mới"
        self._attributes = {}
        self._attr_name = DEFAULT_NAME
        self._attr_unique_id = "vnexpress_news_sensor"
        self._attr_icon = "mdi:newspaper"
        _LOGGER.debug("Hoàn thành khởi tạo sensor")
        self._attr_scan_interval = SCAN_INTERVAL
    async def async_added_to_hass(self):
        """Không khôi phục trạng thái, luôn cập nhật mới khi khởi động."""
        await self.async_update()
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_update(self):
        """Cập nhật sensor."""
        _LOGGER.info("Cập nhật sensor")
        try:
            news, has_new_article = await fetch_rss_news(self.hass, self._gemini_model, self._titles_data, self._api_key)
            # Đếm số lượng tin mới            
            new_count = sum(1 for k in news if "(Tin mới)" in k)
            # Cập nhật trạng thái
            self._state = f"Có {new_count} tin mới" if new_count > 0 else "Không có tin mới"
            # Cập nhật thuộc tính
            def extract_tin_number(key):
              match = re.search(r'Tin (\d+)', key)
              return int(match.group(1)) if match else 9999
            self._attributes = dict(sorted(news.items(), key=lambda x: extract_tin_number(x[0])))  
            self._titles_data = await load_titles(self.hass)
            _LOGGER.info(f"Cập nhật thành công, state: {self._state}, số tin: {len(self._attributes)}")
        except Exception as e:
            _LOGGER.error(f"Lỗi cập nhật sensor: {e}", exc_info=True)
            self._state = "Lỗi"
            self._attributes = {"error": str(e)}
        _LOGGER.debug("Hoàn thành cập nhật")

    @property
    def state(self):
        """Trạng thái sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Thuộc tính sensor."""
        return self._attributes