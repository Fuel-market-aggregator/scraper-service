from fastapi import FastAPI, BackgroundTasks, Query, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import List, Optional
from bs4 import BeautifulSoup
import httpx
import asyncio
from datetime import datetime
import json
import math
import os


app = FastAPI(
    title="Scraper & Business Logic Service",
    root_path="/scraper"
)

IO_SERVICE_URL = os.getenv("IO_SERVICE_URL", "http://io-service:8000")

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8002")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost/auth/token")

class StationResult(BaseModel):
    id: Optional[int] = None
    brand: str
    address: str
    price: float
    distance_km: Optional[float] = None
    efficiency_score: Optional[float] = None

async def get_current_user(token: str = Depends(oauth2_scheme)):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{AUTH_SERVICE_URL}/verify/{token}")
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token invalid sau expirat",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return response.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Serviciul de autentificare nu este disponibil"
            )

async def get_coords_from_address(address_text: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {'q': f"{address_text}, Romania", 'format': 'json', 'limit': 1}
    headers = {'User-Agent': 'FuelPriceApp/1.0 (contact@proiect-idp.ro)'}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, headers=headers, timeout=10.0)
            data = response.json()
            if response.status_code == 200 and len(data) > 0:
                return float(data[0]['lat']), float(data[0]['lon'])
        except Exception as e:
            print(f"Eroare la Geocoding: {e}")
    return None, None

async def scrape_fuel_data(city: str, fuel_type: str):
    url = "https://www.peco-online.ro/index.php"
    payload = {
        'carburant': fuel_type,
        'locatie': 'Oras',
        'nume_locatie': city,
        'retele[]': ['Petrom', 'OMV', 'Lukoil', 'Mol', 'Rompetrol']
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/120.0.0',
        'Referer': 'https://www.peco-online.ro/index.php'
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, data=payload, headers=headers, timeout=15.0)
            soup = BeautifulSoup(response.text, 'html.parser')
            for script in soup.find_all('script'):
                if script.string and "var rezultate = JSON.parse" in script.string:
                    content = script.string
                    start = content.find("('") + 2
                    end = content.find("')")
                    return json.loads(content[start:end])
        except Exception as e:
            print(f"Eroare la Scraping: {e}")
    return []

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

async def save_to_db_background(raw_data, fuel_type: str):
    async with httpx.AsyncClient() as client:
        for s in raw_data:
            brand, s_lat, s_lon, _, address, price = s[0], s[1], s[2], s[3], s[4], s[5]
            
            station_payload = {
                "brand": brand,
                "address": address, 
                "lat": float(s_lat), 
                "lon": float(s_lon)
            }
            try:
                st_res = await client.post(f"{IO_SERVICE_URL}/stations/", json=station_payload)
                if st_res.status_code == 200:
                    station_id = st_res.json().get("id")
                    
                    price_payload = {
                        "station_id": station_id, "fuel_type": fuel_type, "price_value": float(price)
                    }
                    await client.post(f"{IO_SERVICE_URL}/prices/", json=price_payload)
            except Exception as e:
                print(f"Eroare la salvarea în IO-Service: {e}")
                break

@app.get("/rankings", response_model=List[StationResult])
async def get_rankings(
    background_tasks: BackgroundTasks,
    city: str = Query("Bucuresti", description="Orasul de cautare"),
    fuel_type: str = Query("Benzina_Regular", description="Tipul de carburant"),
    limit: int = Query(5, description="Top X rezultate"),
    sort_order: str = Query("asc", description="'asc' pentru ieftin/bun, 'desc' pentru scump/prost"),
    address: Optional[str] = Query(None, description="Adresa pentru calculul distantei (optional)"),
    user: dict = Depends(get_current_user)
):
    raw_data = await scrape_fuel_data(city, fuel_type)
    if not raw_data:
        raise HTTPException(status_code=404, detail="Nu am gasit date.")
    
    background_tasks.add_task(save_to_db_background, raw_data, fuel_type)

    processed_data = []
    
    async with httpx.AsyncClient() as client:
        io_res = await client.get(f"{IO_SERVICE_URL}/stations/")
        existing_stations = {s['address']: s['id'] for s in io_res.json()} if io_res.status_code == 200 else {}

    u_lat, u_lon = (None, None)
    if address:
        u_lat, u_lon = await get_coords_from_address(address)

    for s in raw_data:
        brand, s_lat, s_lon, address_st, price = s[0], float(s[1]), float(s[2]), s[4], float(s[5])
        
        s_id = existing_stations.get(address_st)
        
        dist = calculate_distance(u_lat, u_lon, s_lat, s_lon) if u_lat else None
        score = price + (dist * 0.10) if dist else None
        
        processed_data.append(StationResult(
            id=s_id,
            brand=brand, 
            address=address_st, 
            price=price, 
            distance_km=round(dist, 2) if dist else None, 
            efficiency_score=round(score, 3) if score else None
        ))
            
    reverse_sort = True if sort_order == "desc" else False
    if address:
        processed_data.sort(key=lambda x: x.efficiency_score if x.efficiency_score else 999, reverse=reverse_sort)
    else:
        processed_data.sort(key=lambda x: x.price, reverse=reverse_sort)

    return processed_data[:limit]

@app.get("/history/{station_id}")
async def get_price_history(
    station_id: int, 
    fuel_type: str = Query("Benzina_Regular", description="Tipul de carburant pentru istoric"),
    user: dict = Depends(get_current_user)
):
    async with httpx.AsyncClient() as client:
        try:
            station_res = await client.get(f"{IO_SERVICE_URL}/stations/{station_id}")
            if station_res.status_code != 200:
                raise HTTPException(status_code=404, detail="Statia nu exista.")
            station_info = station_res.json()

            prices_res = await client.get(f"{IO_SERVICE_URL}/stations/{station_id}/prices")
            all_prices = prices_res.json() if prices_res.status_code == 200 else []
            
            history = []
            for p in all_prices:
                if p.get("fuel_type") == fuel_type:
                    raw_date = p.get("timestamp")
                    clean_date = raw_date.split("T")[0] + " " + raw_date.split("T")[1][:5]
                    history.append({"price": p.get("price_value"), "date": clean_date})
            
            return {
                "station_id": station_id,
                "brand": station_info.get("brand"),
                "address": station_info.get("address"),
                "fuel_type": fuel_type,
                "total_records": len(history),
                "history": history
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        
@app.get("/station-status/{station_id}")
async def get_station_status(station_id: int, user: dict = Depends(get_current_user)):
    async with httpx.AsyncClient() as client:
        try:
            station_res = await client.get(f"{IO_SERVICE_URL}/stations/{station_id}")
            if station_res.status_code != 200:
                raise HTTPException(status_code=404, detail="Statia nu exista.")
            station_info = station_res.json()

            response = await client.get(f"{IO_SERVICE_URL}/stations/{station_id}/current-prices")
            
            return {
                "station_id": station_id,
                "brand": station_info.get("brand"),
                "address": station_info.get("address"),
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "current_prices": response.json() if response.status_code == 200 else []
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        
async def periodic_scraper():
    city = "Bucuresti"
    fuel_types = ["Benzina_Regular", "Motorina_Regular"] 
    
    while True:
        for fuel in fuel_types:
            try:
                raw_data = await scrape_fuel_data(city, fuel)
                if raw_data:
                    await save_to_db_background(raw_data, fuel)
                else:
                    print(f"Nu am gasit date pentru {fuel}.")
            except Exception as e:
                print(f"Eroare la procesarea {fuel}: {e}")
        
        await asyncio.sleep(300)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_scraper())