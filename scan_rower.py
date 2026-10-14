# scan_rower.py
import asyncio
from bleak import BleakClient

MAC = "1C:2A:85:46:23:5A"

async def main():
    async with BleakClient(MAC) as client:
        print(f"Connecté : {client.is_connected}")
        for service in client.services:
            print(f"\nService: {service.uuid} — {service.description}")
            for char in service.characteristics:
                print(f"  Char: {char.uuid} | props: {char.properties} | {char.description}")

asyncio.run(main())
