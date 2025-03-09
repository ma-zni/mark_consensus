def parse_chat_response(response: dict) -> str:
    # Return the extracted text from the AI response
    return response.get("choices", [])[0].get("message", {}).get("content", "")
