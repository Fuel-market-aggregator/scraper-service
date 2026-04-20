from fastapi import FastAPI, BackgroundTasks, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from bs4 import BeautifulSoup
import httpx
import asyncio
from datetime import datetime
import json
import math
import os

app = FastAPI(title="Scraper & Business Logic Service")

IO_SERVICE_URL = os.getenv("IO_SERVICE_URL", "http://io-service:8000")

class StationResult(BaseModel):
    brand: str
    address: str
    price: float
    distance_km: Optional[float] = None
    efficiency_score: Optional[float] = None

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
    address: Optional[str] = Query(None, description="Adresa pentru calculul distantei (optional)")
):
    raw_data = await scrape_fuel_data(city, fuel_type)
    if not raw_data:
        raise HTTPException(status_code=404, detail="Nu am gasit date pentru acest oras.")
    
    background_tasks.add_task(save_to_db_background, raw_data, fuel_type)

    processed_data = []
    
    if address:
        u_lat, u_lon = await get_coords_from_address(address)
        if not u_lat:
            raise HTTPException(status_code=400, detail="Nu am putut geocoda adresa.")
            
        PENALTY_FACTOR = 0.10
        for s in raw_data:
            brand, s_lat, s_lon, address_st, price = s[0], float(s[1]), float(s[2]), s[4], float(s[5])
            dist = calculate_distance(u_lat, u_lon, s_lat, s_lon)
            score = price + (dist * PENALTY_FACTOR)
            
            processed_data.append(StationResult(
                brand=brand, address=address_st, price=price, 
                distance_km=round(dist, 2), efficiency_score=round(score, 3)
            ))
            
        processed_data.sort(key=lambda x: x.efficiency_score, reverse=(sort_order == "desc"))

    else:
        for s in raw_data:
            brand, address_st, price = s[0], s[4], float(s[5])
            processed_data.append(StationResult(
                brand=brand, address=address_st, price=price
            ))
            
        processed_data.sort(key=lambda x: x.price, reverse=(sort_order == "desc"))

    return processed_data[:limit]

@app.get("/history/{station_id}")
async def get_price_history(
    station_id: int, 
    fuel_type: str = Query("Benzina_Regular", description="Tipul de carburant pentru istoric")
):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{IO_SERVICE_URL}/stations/{station_id}/prices")
            
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Statia nu a fost gasita sau nu are preturi inregistrate.")
            
            all_prices = response.json()
            
            history = []
            for p in all_prices:
                if p.get("fuel_type") == fuel_type:
                    raw_date = p.get("timestamp")
                    clean_date = raw_date.split("T")[0] + " " + raw_date.split("T")[1][:5]
                    
                    history.append({
                        "price": p.get("price_value"),
                        "date": clean_date
                    })
            
            if not history:
                return {
                    "message": f"Nu exista istoric pentru {fuel_type} la statia cu ID-ul {station_id}.", 
                    "history": []
                }
                
            return {
                "station_id": station_id,
                "fuel_type": fuel_type,
                "total_records": len(history),
                "history": history
            }
            
        except httpx.RequestError as e:
            raise HTTPException(status_code=500, detail=f"Eroare de comunicare cu IO-Service: {e}")
        
@app.get("/station-status/{station_id}")
async def get_station_status(station_id: int):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{IO_SERVICE_URL}/stations/{station_id}/current-prices")
            
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Statia nu are date.")
                
            return {
                "station_id": station_id,
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "current_prices": response.json()
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