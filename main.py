from fastapi import FastAPI, BackgroundTasks
import httpx
import os

app = FastAPI(title="Scraper & Business Logic Service")

IO_SERVICE_URL = os.getenv("IO_SERVICE_URL", "http://io-service:8000")

@app.get("/")
def read_root():
    return {"service": "Scraper Service", "status": "online"}

async def fetch_and_process_prices():
    print("Incepem scraping-ul...")
    
    scraped_data = {
        "station_id": 1,
        "fuel_type": "motorina_standard",
        "price_value": 7.45
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(f"{IO_SERVICE_URL}/prices/", json=scraped_data)
            if response.status_code == 200:
                print("Pret salvat cu succes in DB!")
            else:
                print(f"Eroare la salvare: {response.text}")
        except Exception as e:
            print(f"Nu m-am putut conecta la IO Service: {e}")

@app.post("/trigger-scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
    background_tasks.add_task(fetch_and_process_prices)
    return {"message": "Procesul de scraping a fost pornit in background."}