from argparse import ArgumentParser
import requests
import json
import re

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--address", type=str, default="http://localhost:8000/prompt")
    args = parser.parse_args()

    user_messages = [
        # 'find a video on youtube for kid then checkout hacker news stories'
        # 'check the mail box, is there any news this week?',
        # 'now send an email to mashi@bvm.network, tell him a story about a cat',
        # 'shoot it'
    ]

    user_messages_it = iter(user_messages)
    messages = []
    
    address = args.address
    
    while True:
        message = next(user_messages_it, None)
        
        if callable(message):
            message = message()

        if message is None:
            message = input("\n\nType your message (or 'exit' to quit): ")

        if message.lower() == 'exit':
            break

        print("\n" * 2)
        print("User: ", message, end="", flush=True)

        messages.append({"role": "user", "content": message})
        response = requests.post(
            address, 
            json={"messages": messages},
            stream=True
        )

        with open("chat_history.json", "w") as f:
            json.dump(messages, f, indent=2)

        print("\n" * 2)
        print("Assistant: ", end="", flush=True)
        
        assistant_message = ''

        for chunk in response.iter_lines():
            if chunk and chunk.startswith(b"data: "):
                chunk = chunk[6:]
        
                if chunk == b"[DONE]":
                    break
        
                try:
                    decoded_chunk = chunk.decode("utf-8")
                    json_chunk = json.loads(decoded_chunk)
                    
                    # Handle error responses
                    if "error" in json_chunk:
                        print(f"\nError: {json_chunk['error']}")
                        break
                        
                    # Handle different response formats
                    if "choices" in json_chunk:
                        delta = json_chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        role = delta.get("role")
                        
                        if content:
                            print(content, end="", flush=True)
                            if role in [None, "assistant"]:
                                assistant_message += content
                    elif "message" in json_chunk:
                        print(f"\nMessage: {json_chunk['message']}")
                        
                except json.JSONDecodeError:
                    print(f"\nError decoding response: {decoded_chunk}")
                except Exception as e:
                    print(f"\nError processing response: {str(e)}")
                    continue

        # Filter out the thinking part if present
        if assistant_message:
            # Remove any text that starts with "Okay, let me" or similar thinking phrases
            thinking_patterns = [
                r"^(Okay|Ok|Alright|Sure|Let me|I'll|I will|First|Now|Hmm)[,\s].*?\n",
                r"^I (see|understand|notice|observe|think|believe).*?\n",
                r"^Based on.*?\n",
                r"^Looking at.*?\n",
                r"^Checking.*?\n",
                r"^Searching.*?\n",
                r"^Let's.*?\n"
            ]
            
            filtered_message = assistant_message
            for pattern in thinking_patterns:
                filtered_message = re.sub(pattern, '', filtered_message, flags=re.IGNORECASE | re.MULTILINE)
            
            # Only append if there's content after filtering
            if filtered_message.strip():
                messages.append({"role": "assistant", "content": filtered_message.strip()})

        print("\n" * 2)
