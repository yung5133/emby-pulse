from fastapi import APIRouter, Response
from app.core.config import cfg
from app.core.media_adapter import media_api  # 🔥 引入核心适配器
import requests
import urllib.parse
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re

# 初始化日志
logger = logging.getLogger("uvicorn")
router = APIRouter()

# 🔥 保留一个专门用于外部请求 (如 TMDB) 的 Session
ext_session = requests.Session()
retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
ext_session.mount('http://', HTTPAdapter(max_retries=retries, pool_connections=100, pool_maxsize=100))
ext_session.mount('https://', HTTPAdapter(max_retries=retries, pool_connections=100, pool_maxsize=100))

# 图片 ID 映射缓存
smart_image_cache = {}

def extract_season_number(name: str):
    """从名称中提取季号，例如 '唐朝诡事录 - 第 2 季' -> 2"""
    m = re.search(r'第\s*(\d+)\s*季', name)
    if m: return int(m.group(1))
    m2 = re.search(r'S0*(\d+)', name, re.I)
    if m2: return int(m2.group(1))
    return None

def get_real_image_id_robust(item_id: str):
    """智能 ID 转换（解决剧集封面变单集截图的问题）"""
    try:
        # 🚀 替换为 media_api
        res_a = media_api.get(f"/Items/{item_id}", params={"Fields": "SeriesId,ParentId,SeasonId"}, timeout=3)
        if res_a.status_code == 200:
            data = res_a.json()
            if data.get("Type") == "Episode":
                if data.get("SeasonId"): 
                    season_id = data["SeasonId"]
                    s_res = media_api.get(f"/Items/{season_id}", timeout=2)
                    if s_res.status_code == 200 and s_res.json().get("ImageTags", {}).get("Primary"):
                        return season_id
                if data.get("SeriesId"): return data['SeriesId']
                
            if data.get("SeriesId"): return data['SeriesId']
            if data.get("Type") == "Episode" and data.get("ParentId"): return data['ParentId']
    except: pass

    try:
        res_b = media_api.get(f"/Items/{item_id}/Ancestors", timeout=3)
        if res_b.status_code == 200:
            for ancestor in res_b.json():
                if ancestor.get("Type") == "Series": return ancestor['Id']
                if ancestor.get("Type") == "Season" and not ancestor.get("SeriesId"): return ancestor['Id']
    except: pass

    try:
        res_c = media_api.get("/Items", params={"Ids": item_id, "Fields": "SeriesId", "Recursive": "true"}, timeout=3)
        if res_c.status_code == 200:
            items = res_c.json().get("Items", [])
            if items and items[0].get("SeriesId"): return items[0]['SeriesId']
    except: pass

    return item_id

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    try:
        target_id = get_real_image_id_robust(item_id) if img_type.lower() == 'primary' else item_id
        
        # 🚀 替换为 media_api，并透传 stream=True
        params = {"maxHeight": 600, "maxWidth": 400, "quality": 90}
        resp = media_api.get(f"/Items/{target_id}/Images/{img_type}", params=params, timeout=10, stream=True)
        
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers={"Cache-Control": "public, max-age=3600"})
        
        if resp.status_code == 404 and target_id != item_id:
            fallback_resp = media_api.get(f"/Items/{item_id}/Images/{img_type}", params=params, timeout=10, stream=True)
            if fallback_resp.status_code == 200:
                 return Response(content=fallback_resp.content, media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"), headers={"Cache-Control": "public, max-age=3600"})
    except Exception: pass
    return Response(status_code=404)

@router.get("/api/proxy/smart_image")
def proxy_smart_image(item_id: str, name: str = "", year: str = "", type: str = "Primary"):
    # 1. 缓存拦截 (外部链接仍使用 ext_session)
    cached_result = smart_image_cache.get(item_id)
    if cached_result and str(cached_result).startswith('http'):
        try:
            proxy = cfg.get("proxy_url")
            proxies = {"https": proxy, "http": proxy} if proxy else None
            resp = ext_session.get(cached_result, proxies=proxies, timeout=10, stream=True)
            if resp.status_code == 200:
                return Response(content=resp.content, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
        except Exception as e:
            logger.error(f"从缓存获取 TMDB 图片失败: {e}")
            pass 

    target_id = cached_result if cached_result and not str(cached_result).startswith('http') else item_id
    img_type = type
    params = {"maxWidth": 1920, "quality": 80} if img_type.lower() == 'backdrop' else {"maxHeight": 800, "maxWidth": 600, "quality": 90}
    
    if img_type.lower() == 'primary' and target_id == item_id:
        target_id = get_real_image_id_robust(target_id)
        
    # 2. 第 1 级防御：正常请求媒体库 (使用 media_api)
    try:
        resp = media_api.get(f"/Items/{target_id}/Images/{img_type}", params=params, timeout=5, stream=True)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers={"Cache-Control": "public, max-age=86400"})
    except requests.exceptions.RequestException as e: 
        logger.debug(f"媒体库图片请求超时或断开: {e}")

    # 3. 第 2 级防御：洗版名字搜索兜底 (使用 media_api)
    clean_name = name.split(' - ')[0].strip() if name else ""
    if clean_name:
        try:
            s_resp = media_api.get("/Items", params={"SearchTerm": clean_name, "IncludeItemTypes": "Movie,Series,Episode", "Recursive": "true"}, timeout=5)
            if s_resp.status_code == 200:
                items = s_resp.json().get("Items", [])
                if items:
                    new_id = items[0]["Id"]
                    if items[0]["Type"] in ["Episode", "Season", "Series"]:
                        new_id = get_real_image_id_robust(new_id)
                    smart_image_cache[item_id] = new_id 
                    
                    n_resp = media_api.get(f"/Items/{new_id}/Images/{img_type}", params=params, timeout=5, stream=True)
                    if n_resp.status_code == 200:
                        return Response(content=n_resp.content, media_type=n_resp.headers.get("Content-Type", "image/jpeg"), headers={"Cache-Control": "public, max-age=86400"})
        except requests.exceptions.RequestException: pass

    # 4. 第 3 级防御：TMDB 终极兜底 (外部请求，保留 ext_session)
    tmdb_key = cfg.get("tmdb_api_key")
    season_num = extract_season_number(name)

    if clean_name and tmdb_key:
        try:
            proxy = cfg.get("proxy_url")
            proxies = {"https": proxy, "http": proxy} if proxy else None
            
            tmdb_url = f"https://api.tmdb.org/3/search/multi?api_key={tmdb_key}&language=zh-CN&query={urllib.parse.quote(clean_name)}"
            t_resp = ext_session.get(tmdb_url, proxies=proxies, timeout=5)
            
            if t_resp.status_code == 200:
                results = t_resp.json().get("results", [])
                for res in results:
                    if res.get("media_type") == "tv" and season_num is not None and img_type.lower() == 'primary':
                        tv_id = res.get("id")
                        season_url = f"https://api.tmdb.org/3/tv/{tv_id}/season/{season_num}?api_key={tmdb_key}&language=zh-CN"
                        s_resp = ext_session.get(season_url, proxies=proxies, timeout=5)
                        if s_resp.status_code == 200:
                            s_data = s_resp.json()
                            if s_data.get("poster_path"):
                                final_url = f"https://image.tmdb.org/t/p/w500{s_data['poster_path']}"
                                smart_image_cache[item_id] = final_url
                                final_resp = ext_session.get(final_url, proxies=proxies, timeout=8, stream=True)
                                if final_resp.status_code == 200:
                                    return Response(content=final_resp.content, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

                    if res.get("media_type") in ["movie", "tv"]:
                        img_path = res.get("backdrop_path") if img_type.lower() == 'backdrop' else res.get("poster_path")
                        if img_path:
                            tmdb_img_url = f"https://image.tmdb.org/t/p/w500{img_path}"
                            smart_image_cache[item_id] = tmdb_img_url 
                            
                            final_resp = ext_session.get(tmdb_img_url, proxies=proxies, timeout=8, stream=True)
                            if final_resp.status_code == 200:
                                return Response(content=final_resp.content, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
                        break
        except requests.exceptions.RequestException as e:
            logger.error(f"TMDB 兜底网络异常 [{clean_name}]: {e}")
            
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    try:
        params = {"width": 200, "height": 200, "mode": "Crop", "quality": 90}
        if tag: params["tag"] = tag
        # 🚀 替换为 media_api
        resp = media_api.get(f"/Users/{user_id}/Images/Primary", params=params, timeout=3, stream=True)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except: pass
    return Response(status_code=404)