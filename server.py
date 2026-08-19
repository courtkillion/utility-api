from dotenv import load_dotenv

load_dotenv()

from cdp.x402 import create_facilitator_config
from fastapi import FastAPI
from x402.http import HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

NETWORK = "eip155:84532"          # Base Sepolia
PAY_TO = "0x28Bf61F081331Bf69DA51688B6fC802677Ab4AC8"

server = x402ResourceServer(HTTPFacilitatorClient(create_facilitator_config()))
server.register(NETWORK, ExactEvmServerScheme())

routes = {
    "GET /v1/pod/price": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact", pay_to=PAY_TO, price="$0.001", network=NETWORK
            )
        ],
        mime_type="application/json",
        description="POD listing price solver",
    )
}

app = FastAPI()
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


@app.get("/v1/pod/price")
async def pod_price() -> dict:
    return {"listing_price": 32.00, "net_profit": 12.00}