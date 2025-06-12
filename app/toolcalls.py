from browser_use import Controller, Browser, ActionResult, BrowserConfig, BrowserContext
import fnmatch
import json
import asyncio
from datetime import datetime

# Add test log to verify logging is working
print("=== LinkedIn Script Started ===")

browser = Browser(
    config=BrowserConfig(
        chrome_instance_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
)

async def navigate_to_linkedln(ctx : BrowserContext) -> None:
    page = await ctx.get_current_page()
    str_destination = "https://www.linkedin.com/"
    await page.goto(str_destination, wait_until="domcontentloaded")

async def ensure_linkedln(ctx : BrowserContext) -> None:
    page = await ctx.get_current_page()
    str_destination = "https://www.linkedin.com/"
    current_url = page.url
    if not fnmatch.fnmatch(current_url, str_destination + "*"):
        await navigate_to_linkedln(ctx)


async def auto_sign_in(ctx: BrowserContext):
    accountemail = "huy.20080119.606@gmail.com"
    accountpassword = "Bruh98012"
    page = await ctx.get_current_page()
    await page.wait_for_load_state("domcontentloaded")
    google_btn = page.locator('div.nsm7Bb-HzV7m-LgbsSe[role="button"]', has_text="Continue with Google")
    await google_btn.wait_for()
    await google_btn.click()
    popup_page = await page.wait_for_event('popup')
    await popup_page.wait_for_load_state("domcontentloaded")
    await popup_page.locator('input[type="email"]').fill(accountemail)
    await popup_page.locator('text=Next').click()
    await popup_page.locator('input[type="password"]').fill(accountpassword)
    await popup_page.locator('text=Next').click()

async def navigate_to_messaging(ctx: BrowserContext):
    page = await ctx.get_current_page()
    messaging_button = page.locator('a.global-nav__primary-link[href*="/messaging/"]')
    await messaging_button.wait_for()
    await messaging_button.click()

async def fetch_profile_in_message(ctx: BrowserContext):
    """
    Fetches profiles from LinkedIn messaging using confirmed working selectors.
    Returns and saves a JSON structure containing profile information and interaction data.
    """
    print("\n=== Fetching Profiles from Messages ===")
    page = await ctx.get_current_page()
    
    # Initialize the result structure
    result = {
        "profiles": [],
        "metadata": {
            "total_count": 0,
            "fetch_time": datetime.now().isoformat(),
            "container_selector": "div.msg-overlay-list-bubble"
        }
    }
    
    try:
        # Wait for the messaging overlay - confirmed working from analysis
        messaging_container = page.locator('div.msg-overlay-list-bubble')
        await messaging_container.wait_for(state="visible", timeout=10000)
        
        # Wait for the conversation list - confirmed working from analysis
        conversation_list = page.locator('.msg-conversations-container__conversations-list')
        await conversation_list.wait_for(state="visible", timeout=10000)
        
        # Get all conversation threads using the most reliable selector
        thread_elements = page.locator('.msg-conversation-card')
        thread_count = await thread_elements.count()
        print(f"Found {thread_count} conversations")
        
        result["metadata"]["total_count"] = thread_count
        
        # First gather all basic information without clicking
        for i in range(thread_count):
            try:
                thread = thread_elements.nth(i)
                
                # Get name
                name_element = thread.locator('.msg-conversation-card__participant-names')
                name = await name_element.text_content()
                name = name.strip()
                
                # Check for unread messages
                unread_indicator = thread.locator('.msg-conversation-card__unread-count')
                has_unread = await unread_indicator.count() > 0
                
                # Try to get last message time
                try:
                    time_element = thread.locator('.msg-conversation-card__time-stamp')
                    last_message_time = await time_element.text_content()
                    last_message_time = last_message_time.strip()
                except:
                    last_message_time = None
                
                # Store initial profile info
                profile_data = {
                    "id": i + 1,
                    "name": name,
                    "thread_link": None,  # Will be updated later
                    "last_message_time": last_message_time,
                    "unread": has_unread,
                    "element_selectors": {
                        "thread": ".msg-conversation-card",
                        "name": ".msg-conversation-card__participant-names",
                        "index": i
                    }
                }
                
                result["profiles"].append(profile_data)
                print(f"✓ Gathered basic info for {i+1}/{thread_count}: {name}")
                
            except Exception as e:
                print(f"Error gathering info for conversation {i+1}: {str(e)}")
                continue
        
        # Now get thread links with optimized timing
        for i, profile in enumerate(result["profiles"]):
            try:
                thread = thread_elements.nth(profile["element_selectors"]["index"])
                
                # Click the thread with a shorter timeout
                await thread.click()
                
                # Wait for URL to change with a shorter timeout
                try:
                    await page.wait_for_url("**/messaging/thread/**", timeout=5000)
                    profile["thread_link"] = page.url
                    print(f"✓ Got thread link for: {profile['name']}")
                except Exception as e:
                    print(f"Timeout getting thread link for {profile['name']}, using fallback method")
                    profile["thread_link"] = page.url
                
                # Quick check if we need to go back
                if i < len(result["profiles"]) - 1:
                    await conversation_list.click()
                    await asyncio.sleep(1)  # Short pause between operations
                
            except Exception as e:
                print(f"Error getting thread link for {profile['name']}: {str(e)}")
                continue
        
        print(f"\nSuccessfully processed {len(result['profiles'])} profiles")
        
        # Save the result to a JSON file
        with open('linkedin_messages.json', 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print("Profile data saved to linkedin_messages.json")
        
        return result
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "profiles": [],
            "metadata": {
                "total_count": 0,
                "fetch_time": datetime.now().isoformat(),
                "error": str(e)
            }
        }

async def is_logged_in(ctx : BrowserContext):
    await ensure_linkedln(ctx)
    page = await ctx.get_current_page()
    await page.wait_for_load_state("domcontentloaded")
    try:
        if await page.locator('a[data-tracking-control-name="guest_homepage-basic_nav-header-signin"]').is_visible():
            print("Not logged in")
            return False 
        print("Logged in")
        return True
    except Exception as e:
        print(e)
        return False


async def select_conversation(matches: list) -> dict:
    """
    Prompts user to select a conversation when multiple matches are found.
    
    Args:
        matches (list): List of matching conversations
        
    Returns:
        dict: Selected conversation or None if invalid input
    """
    print("\n[Select] Multiple matching conversations found:")
    for i, conv in enumerate(matches):
        unread = "🔵" if conv["unread"] else "  "
        time = conv["last_message_time"] or "no time"
        print(f"{i + 1}. {unread} {conv['name']} ({time})")
        if conv["metadata"]["is_group"]:
            print(f"   Group chat with {conv['metadata']['participant_count']} participants")
    
    try:
        choice = input("\n[Select] Enter number to choose conversation (or press Enter to cancel): ")
        if not choice:
            return None
            
        idx = int(choice) - 1
        if 0 <= idx < len(matches):
            return matches[idx]
        else:
            print("[Error] Invalid selection")
            return None
    except ValueError:
        print("[Error] Please enter a valid number")
        return None

async def filter_threads_by_name(name_query: str) -> list:
    """
    Filter threads by name from the saved JSON data.
    Returns a list of matching conversations.
    
    Args:
        name_query (str): Name or partial name to search for (case-insensitive)
    
    Returns:
        list: List of matching conversation dictionaries
    """
    try:
        # Load the saved JSON data
        with open('conversation_list.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Filter conversations where name contains the query (case-insensitive)
        matches = [
            conv for conv in data["conversations"] 
            if name_query.lower() in conv["name"].lower()
        ]
        
        # Print results
        if matches:
            print(f"\n[Search] Found {len(matches)} matching conversations:")
            for conv in matches:
                unread = "🔵" if conv["unread"] else "  "
                time = conv["last_message_time"] or "no time"
                print(f"{unread} [{conv['index']}] {conv['name']} ({time})")
                if conv["metadata"]["is_group"]:
                    print(f"    Group chat with {conv['metadata']['participant_count']} participants")
                    
            # If multiple matches, let user select one
            if len(matches) > 1:
                selected = await select_conversation(matches)
                if selected:
                    return [selected]  # Return single-item list for compatibility
                else:
                    print("[Info] Conversation selection cancelled")
                    return []
        else:
            print(f"[Search] No conversations found matching '{name_query}'")
            
        return matches
        
    except FileNotFoundError:
        print("[Error] No saved conversation data found. Please fetch conversations first.")
        return []
    except json.JSONDecodeError as e:
        print(f"[Error] Invalid JSON in conversation data: {str(e)}")
        return []
    except Exception as e:
        print(f"[Error] Error filtering conversations: {str(e)}")
        return []

async def enter_conversation_by_id(ctx: BrowserContext, thread_id: int):
    """
    Enter a specific conversation by its ID.
    Throws an error if no thread or multiple threads match the ID.
    
    Args:
        ctx (BrowserContext): Browser context
        thread_id (int): ID of the thread to enter (matches the index)
        
    Raises:
        ValueError: If no thread or multiple threads match the ID
    """
    try:
        # Load the saved JSON data
        with open('conversation_list.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Find the exact thread using index
        matches = [c for c in data["conversations"] if c["index"] == thread_id]
        
        if len(matches) == 0:
            raise ValueError(f"[Error] No conversation found with ID {thread_id}")
        elif len(matches) > 1:
            raise ValueError(f"[Error] Multiple conversations found with ID {thread_id}")
        
        # Use enter_conversation_directly since it has better error handling
        return await enter_conversation_directly(ctx, matches)
        
    except FileNotFoundError:
        raise ValueError("[Error] No saved conversation data found. Please fetch conversations first.")
    except Exception as e:
        raise ValueError(f"[Error] Failed to enter conversation: {str(e)}")

async def enter_conversation_directly(ctx: BrowserContext, conversation_data: list) -> bool:
    """
    Enters a conversation using the provided conversation data.
    Expects exactly one conversation in the list.
    
    Args:
        ctx (BrowserContext): Browser context
        conversation_data (list): List containing exactly one conversation dictionary
        
    Returns:
        bool: True if successfully entered conversation, False otherwise
        
    Raises:
        ValueError: If conversation_data is empty or contains multiple conversations
    """
    print("[Enter] Validating conversation data...")
    
    # Validate input
    if not conversation_data:
        raise ValueError("[Error] No conversation data provided")
    if len(conversation_data) > 1:
        raise ValueError(f"[Error] Expected 1 conversation, got {len(conversation_data)}")
    
    conversation = conversation_data[0]
    page = await ctx.get_current_page()
    
    try:
        # Ensure we're on messaging page
        messaging_container = page.locator('div.msg-overlay-list-bubble')
        if not await messaging_container.is_visible():
            print("[Enter] Opening messaging page...")
            await navigate_to_messaging(ctx)
            await messaging_container.wait_for(state="visible", timeout=10000)
        
        print(f"[Enter] Attempting to enter conversation with: {conversation['name']}")
        
        # Use the stored selector to find the conversation
        conversation_cards = page.locator(conversation['selectors']['card'])
        count = await conversation_cards.count()
        
        if count == 0:
            print("[Enter] No conversations visible, checking alternative selectors...")
            alternative_selectors = [
                'li.msg-conversation-listitem',
                '.msg-conversation-listitem__link',
                '.msg-selectable-entity'
            ]
            
            for selector in alternative_selectors:
                conversation_cards = page.locator(selector)
                count = await conversation_cards.count()
                if count > 0:
                    print(f"[Enter] Found conversations using: {selector}")
                    break
        
        if conversation['index'] >= count:
            raise ValueError(f"[Error] Conversation index {conversation['index']} out of range (total: {count})")
            
        # Get the specific conversation card
        card = conversation_cards.nth(conversation['index'])
        
        # Verify we're clicking the right conversation
        name_element = card.locator(conversation['selectors']['name'])
        if await name_element.count() > 0:
            actual_name = await name_element.text_content()
            actual_name = actual_name.strip()
            if actual_name != conversation['name']:
                print(f"[Warning] Name mismatch: Expected '{conversation['name']}', found '{actual_name}'")
        
        # Click the conversation
        print("[Enter] Clicking conversation...")
        await card.click()
        await page.wait_for_url("**/messaging/thread/**", timeout=5000)
        
        print(f"[Success] Entered conversation with {conversation['name']}")
        if conversation['metadata']['is_group']:
            print(f"[Info] This is a group chat with {conversation['metadata']['participant_count']} participants")
        if conversation['unread']:
            print("[Info] Conversation has unread messages")
            
        return True
        
    except Exception as e:
        print(f"[Error] Failed to enter conversation: {str(e)}")
        return False

async def fetch_conversation_list(ctx: BrowserContext, limit: int = 30) -> dict:
    """
    Quickly fetches a list of available conversations and their metadata.
    Returns a structured JSON format that's easily expandable.
    
    Args:
        ctx (BrowserContext): Browser context
        limit (int, optional): Maximum number of conversations to fetch. Defaults to 30.
        
    Returns:
        dict: Structured conversation data
    """
    page = await ctx.get_current_page()
    print(f"[Fetch] Getting conversation list (limit: {limit})...")
    
    result = {
        "conversations": [],
        "summary": {
            "total_count": 0,
            "unread_count": 0,
            "fetch_time": datetime.now().isoformat(),
            "version": "1.0",
            "limit_applied": limit
        }
    }
    
    try:
        # Wait for the messaging container to be visible
        print("[Fetch] Waiting for messaging container...")
        messaging_container = page.locator('div.msg-overlay-list-bubble')
        await messaging_container.wait_for(state="visible", timeout=10000)
        
        # Wait for the conversation list to be visible
        print("[Fetch] Waiting for conversation list...")
        conversation_list = page.locator('.msg-conversations-container__conversations-list')
        await conversation_list.wait_for(state="visible", timeout=10000)
        
        # Give a short time for dynamic content to load
        await asyncio.sleep(2)
        
        # Find all conversation cards
        conversation_cards = page.locator('.msg-conversation-card')
        total_count = await conversation_cards.count()
        
        if total_count == 0:
            # Try alternative selectors if the first one returns no results
            print("[Fetch] No conversations found with primary selector, trying alternatives...")
            alternative_selectors = [
                'li.msg-conversation-listitem',
                '.msg-conversation-listitem__link',
                '.msg-selectable-entity'
            ]
            
            for selector in alternative_selectors:
                conversation_cards = page.locator(selector)
                total_count = await conversation_cards.count()
                if total_count > 0:
                    print(f"[Fetch] Found {total_count} conversations using alternative selector: {selector}")
                    break
        
        if total_count == 0:
            print("[Fetch] Still no conversations found. Taking debug screenshot...")
            await page.screenshot(path="debug_messaging_page.png")
            print("[Fetch] Debug screenshot saved as debug_messaging_page.png")
            return result
            
        # Apply limit
        count_to_process = min(total_count, limit)
        print(f"[Fetch] Processing {count_to_process} out of {total_count} conversations...")
        unread_count = 0
        
        def truncate_text(text, max_length=40):
            """Helper to truncate and clean text"""
            if not text:
                return None
            # Clean up whitespace and remove newlines
            text = " ".join(text.split())
            if len(text) <= max_length:
                return text
            return text[:max_length-3] + "..."
        
        # Process each conversation up to the limit
        for i in range(count_to_process):
            try:
                card = conversation_cards.nth(i)
                
                # Get name (try multiple selectors)
                name = ""
                name_selectors = [
                    '.msg-conversation-card__participant-names',
                    '.msg-conversation-listitem__participant-names',
                    '.msg-selectable-entity__content'
                ]
                
                for selector in name_selectors:
                    name_element = card.locator(selector)
                    if await name_element.count() > 0:
                        name = await name_element.text_content()
                        name = truncate_text(name, 25)  # Truncate name
                        if name:
                            break
                
                if not name:
                    print(f"[Warning] Could not find name for conversation {i}")
                    continue
                
                # Check unread status
                unread_indicator = card.locator('.msg-conversation-card__unread-count')
                has_unread = await unread_indicator.count() > 0
                if has_unread:
                    unread_count += 1
                
                # Get last message time
                time_element = card.locator('.msg-conversation-card__time-stamp')
                last_message_time = await time_element.text_content() if await time_element.count() > 0 else None
                
                # Get recent message preview
                message_preview_selectors = [
                    '.msg-conversation-card__message-snippet',
                    '.msg-conversation-listitem__message-snippet',
                    'p[class*="message-snippet"]'
                ]
                
                recent_message = None
                for selector in message_preview_selectors:
                    try:
                        preview_element = card.locator(selector)
                        if await preview_element.count() > 0:
                            recent_message = await preview_element.text_content()
                            recent_message = truncate_text(recent_message, 40)  # Truncate message
                            if recent_message:
                                break
                    except Exception:
                        continue
                
                # Check if it's a group conversation
                participant_count = len(name.split(',')) if name else 1
                is_group = participant_count > 1
                
                # Check for pending invites
                pending_invite = card.locator('.msg-conversation-card__invite-pending-status')
                has_pending = await pending_invite.count() > 0
                
                conversation = {
                    "name": name,
                    "unread": has_unread,
                    "last_message_time": last_message_time.strip() if last_message_time else None,
                    "recent_message": recent_message,
                    "index": i,
                    "selectors": {
                        "card": ".msg-conversation-card",
                        "name": name_selectors[0]
                    },
                    "metadata": {
                        "is_group": is_group,
                        "participant_count": participant_count,
                        "has_pending_invites": has_pending,
                        "truncated": True,  # Flag to indicate data is pre-truncated
                        "name_max_length": 25,
                        "message_max_length": 40
                    }
                }
                
                result["conversations"].append(conversation)
                print(f"[Fetch] Found conversation: {name}")
                
            except Exception as e:
                print(f"[Warning] Error processing conversation {i}: {str(e)}")
                continue
        
        # Update summary with both total and processed counts
        result["summary"].update({
            "total_available": total_count,
            "total_processed": len(result["conversations"]),
            "unread_count": unread_count,
            "limit_reached": total_count > limit
        })
        
        # Save to file for future use
        with open('conversation_list.json', 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        
        print(f"[Fetch] Processed {len(result['conversations'])} out of {total_count} conversations ({unread_count} unread)")
        if total_count > limit:
            print(f"[Fetch] Note: {total_count - limit} conversations were not processed due to limit")
        return result
        
    except Exception as e:
        print(f"[Error] Failed to fetch conversation list: {str(e)}")
        await page.screenshot(path="error_messaging_page.png")
        print("[Error] Debug screenshot saved as error_messaging_page.png")
        return result

async def run():
    try:
        await browser.start()
        print("Browser started")
        
        await navigate_to_linkedln(browser)
        print("Navigated to LinkedIn")
        
        print("Running... Press Ctrl+C to shutdown")
        
        # if not await is_logged_in(browser):
        #     print("Signing in...")
        #     await auto_sign_in(browser)
        #     print("Sign in completed")
        
        # # Direct conversation entry without profile fetching
        await navigate_to_messaging(browser)
        
        # First fetch the conversation list
        print("\nFetching conversation list...")
        conversations = await fetch_conversation_list(browser)
        
        if not conversations or not conversations.get("conversations"):
            print("[Error] Failed to fetch conversations")
            return
            
        print("\nAvailable conversations:")
        for conv in conversations["conversations"]:
            unread = "🔵" if conv["unread"] else "  "
            time = conv["last_message_time"] or "no time"
            message = conv["recent_message"] or "no preview"
            
            # Print conversation header with name and time
            print(f"{unread} {conv['name']:<25} ({time})")
            print(f"    └─ {message}")
            print()
        
        # Example: Search for a specific conversation
        print("\nSearching for a specific conversation...")
        partipant_name = input("\n[Input] Enter name to search for (or press Enter to exit): ")
        if not partipant_name:
            print("\nExiting...")
            await browser.close()
            return
        
        # Verify the conversation list file exists and is valid before searching
        try:
            with open('conversation_list.json', 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
                if not saved_data.get("conversations"):
                    print("[Error] Invalid conversation data, fetching again...")
                    conversations = await fetch_conversation_list(browser)
        except (FileNotFoundError, json.JSONDecodeError):
            print("[Error] Conversation data not found or invalid, fetching again...")
            conversations = await fetch_conversation_list(browser)
        
        matches = await filter_threads_by_name(partipant_name)
        
        if matches:
            try:
                print(f"\n[Action] Entering conversation with {matches[0]['name']}...")
                if await enter_conversation_directly(browser, matches):
                    print("[Ready] Conversation is ready for interaction")
                else:
                    print("[Error] Failed to enter conversation")
            except ValueError as e:
                print(f"[Error] {str(e)}")
        else:
            print(f"[Info] No conversations found with '{partipant_name}'")
        
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down")
            await browser.close()
            
    except Exception as e:
        print(f"\nError: {str(e)}")
        await browser.close()

def main():
    asyncio.run(run())

main()
