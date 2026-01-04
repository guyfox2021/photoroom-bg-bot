import aiohttp

API_URL = "https://image-api.photoroom.com/v2/edit"


async def remove_bg(image_bytes: bytes, api_key: str) -> bytes:
    headers = {"x-api-key": api_key}

    form = aiohttp.FormData()
    form.add_field(
        name="imageFile",
        value=image_bytes,
        filename="image.png",
        content_type="image/png",
    )

    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(API_URL, headers=headers, data=form) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"PhotoRoom API error {resp.status}: {text}")
            return await resp.read()
