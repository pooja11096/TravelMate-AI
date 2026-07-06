from mcp.server.fastmcp import FastMCP

mcp = FastMCP("TravelMateMCP")

@mcp.tool()
def get_flight_status(flight_number: str) -> str:
    """Gets the simulated real-time status of a flight.
    
    Args:
        flight_number: The flight number (e.g. DL123).
    """
    return f"Flight {flight_number} is on time."

@mcp.tool()
def get_currency_exchange(base: str, target: str) -> str:
    """Gets the current exchange rate from base to target currency.
    
    Args:
        base: The base currency code (e.g. USD).
        target: The target currency code (e.g. JPY).
    """
    rates = {"USD-JPY": 150.5, "USD-EUR": 0.92, "EUR-USD": 1.09, "JPY-USD": 0.0066}
    key = f"{base.upper()}-{target.upper()}"
    rate = rates.get(key, 1.0)
    return f"Current exchange rate: 1 {base.upper()} = {rate} {target.upper()}."

@mcp.tool()
def get_weather_forecast(location: str, days: int = 3) -> str:
    """Gets the weather forecast for a location.
    
    Args:
        location: The city or region.
        days: Number of days to forecast.
    """
    return f"Forecast for {location} over {days} days: Mostly sunny with highs in the mid-70s F."

@mcp.tool()
def get_local_attractions(location: str) -> str:
    """Gets popular attractions for a specific destination.
    
    Args:
        location: The destination city.
    """
    return f"Top attractions in {location}: Historical Downtown, The Grand Museum, Local Street Market, and Scenic Park."

if __name__ == "__main__":
    mcp.run()
