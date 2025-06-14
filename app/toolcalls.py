from browser_use.browser.context import BrowserContext
from playwright.async_api._generated import ElementHandle, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from typing import Optional, Generic, TypeVar, Any
from .controllers import (
    check_authorization,
    ensure_url,
    get_login_status
)
from .signals import UnauthorizedAccess
from pydantic import BaseModel, model_validator
import logging
import json
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

_generic_type = TypeVar('_generic_type')
class ResponseMessage(BaseModel, Generic[_generic_type]):
    result: Optional[_generic_type] = None
    error: Optional[str] = None
    success: bool = True

    @model_validator(mode="after")
    def refine_status(self):
        if self.error is not None:
            self.success = False
        return self

async def ensure_authorized(ctx: BrowserContext) -> bool:
    if not await check_authorization(ctx):
        await ensure_url(ctx, 'https://www.linkedin.com/')
        raise UnauthorizedAccess('You are not authorized to access this resource. Please log in to your LinkedIn account.')
    await ensure_url(ctx, 'https://www.linkedin.com/')
    return True

async def sign_out(ctx: BrowserContext) -> ResponseMessage[bool]:
    response_model = ResponseMessage[bool]
    if not await check_authorization(ctx):
        return response_model(result=True)
    page = await ctx.get_current_page()
    await page.goto('https://www.linkedin.com/m/logout')
    return response_model(result=True)

async def check_login_status(ctx: BrowserContext) -> ResponseMessage[dict]:
    """Check the current LinkedIn login status and return profile information if available."""
    response_model = ResponseMessage[dict]
    try:
        status = await get_login_status(ctx)
        return response_model(result=status)
    except Exception as e:
        return response_model(error=str(e), success=False)

async def get_context_aware_available_toolcalls(ctx: BrowserContext):
    toolcalls = [
        {
            'type': 'function',
            'function': {
                'name': 'sign_out',
                'description': 'Sign out from the current LinkedIn session.',
                'parameters': {},
                'strict': False
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'check_login_status',
                'description': 'Check if the user is logged into LinkedIn and get profile information.',
                'parameters': {},
                'strict': False
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'read_full_conversation',
                'description': 'Reads all messages in the currently open LinkedIn conversation thread.',
                'parameters': {},
                'strict': False
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'fetch_profile_in_message',
                'description': 'Fetches all profiles from LinkedIn messaging, including conversation metadata and thread links.',
                'parameters': {},
                'strict': False
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'enter_conversation_directly',
                'description': 'Enters a specific conversation. IMPORTANT: First use fetch_profile_in_message to get the list of conversations, then call this with the exact name.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'target_name': {
                            'type': 'string',
                            'description': 'Exact name of the contact to chat with (must match the name from fetch_profile_in_message)'
                        }
                    },
                    'required': ['target_name'],
                    'additionalProperties': False
                },
                'strict': True
            }
        }
    ]

    is_authorized = await check_authorization(ctx)

    if is_authorized:
        # Return all tools except sign_out when authorized
        return [tool for tool in toolcalls if tool['function']['name'] != 'sign_out']
    else:
        # Return only sign_out and check_login_status when not authorized
        allowed_unauthorized = ['sign_out', 'check_login_status']
        return [tool for tool in toolcalls if tool['function']['name'] in allowed_unauthorized]

async def execute_toolcall(
    ctx: BrowserContext, 
    tool_name: str, 
    args: dict[str, Any]
) -> ResponseMessage[Any]:
    response_model = ResponseMessage[Any]

    if tool_name == "check_login_status":
        return await check_login_status(ctx)
    elif tool_name == "sign_out":
        return await sign_out(ctx)
    elif tool_name == "read_full_conversation":
        return await read_full_conversation(ctx)
    elif tool_name == "fetch_profile_in_message":
        return await fetch_profile_in_message(ctx)
    elif tool_name == "enter_conversation_directly":
        return await enter_conversation_directly(ctx, args.get("target_name"))
    else:
        return response_model(error=f"Unknown tool call: {tool_name}", success=False)

async def get_current_user_identity(
    ctx: BrowserContext
) -> ResponseMessage[str]:
    """Get the current user's identity from their LinkedIn profile."""
    response_model = ResponseMessage[str]

    if not await check_authorization(ctx):
        return response_model(error="User is not authorized.", success=False)

    page = await ctx.get_current_page()
    url = page.url.strip("/")

    if not url.startswith('https://www.linkedin.com'):
        return response_model(error="User is not on LinkedIn page.", success=False)

    # Update selector for LinkedIn profile
    element = await page.query_selector('.global-nav__me-photo')

    if not element:
        return response_model(error="Failed to find the user identity element.", success=False)

    user_identity = await element.get_attribute('alt')
    return response_model(result=user_identity)

import re
from datetime import datetime, timedelta

async def read_full_conversation(ctx: BrowserContext) -> ResponseMessage[list[dict]]:
    """
    Reads all messages in the currently open LinkedIn conversation thread,
    capturing sender, message text, and specific datetime (from date separators + per-message time).
    """
    response_model = ResponseMessage[list[dict]]

    if not await check_authorization(ctx):
        return response_model(error="User is not authorized.", success=False)

    try:
        page = await ctx.get_current_page()
        await page.wait_for_selector("li.msg-s-message-list__event", timeout=15_000)

        # All message blocks (date dividers + bubbles)
        events = await page.query_selector_all("li.msg-s-message-list__event")

        results = []
        current_date = None
        last_time = None

        for event in events:
            # Check if this is a date divider
            date_el = await event.query_selector("span.msg-s-date-divider__date")
            if date_el:
                raw_date = (await date_el.inner_text()).strip()

                # Parse date string
                today = datetime.now()
                if raw_date.lower() == "today":
                    current_date = today.date()
                elif raw_date.lower() == "yesterday":
                    current_date = (today - timedelta(days=1)).date()
                else:
                    try:
                        current_date = datetime.strptime(raw_date, "%b %d").replace(year=today.year).date()
                    except ValueError:
                        current_date = None
                continue

            # Find all message bubbles in this block
            bubbles = await event.query_selector_all("div.msg-s-event-listitem__message-bubble")
            for bubble in bubbles:
                # Message text
                text_el = await bubble.query_selector("p")
                text = (await text_el.inner_text()).strip() if text_el else ""

                # Sender
                parent = await bubble.evaluate_handle("node => node.parentElement")
                class_list = await parent.evaluate("node => node.className")
                sender = "them" if "msg-s-event-listitem--other" in class_list else "me"

                # Time (if any)
                timestamp_el = await bubble.query_selector("span.msg-s-message-group__timestamp")
                time_str = (await timestamp_el.inner_text()).strip() if timestamp_el else None
                if time_str:
                    last_time = time_str

                # Combine current_date + last_time
                if current_date and last_time:
                    try:
                        full_datetime = datetime.strptime(f"{current_date} {last_time}", "%Y-%m-%d %I:%M %p")
                        datetime_str = full_datetime.isoformat()
                    except Exception:
                        datetime_str = None
                else:
                    datetime_str = None

                if text:
                    results.append({
                        "sender": sender,
                        "text": text,
                        "datetime": datetime_str
                    })

        return response_model(result=results)

    except PlaywrightTimeoutError:
        return response_model(error="Timeout waiting for messages to load", success=False)
    except Exception as e:
        logger.error(f"Error reading conversation: {str(e)}")
        return response_model(error=f"Failed to read conversation: {str(e)}", success=False)

async def fetch_profile_in_message(ctx: BrowserContext) -> ResponseMessage[dict]:
    """
    Fetches basic message preview information from LinkedIn messaging.
    Only retrieves:
    - Name of the contact
    - Last message timestamp
    - Message preview text (if available)
    
    This function does NOT:
    - Click on any profiles
    - Navigate to conversation threads
    - Collect additional profile information
    - Save any files locally
    
    Returns a simple list of message previews with minimal information.
    Use this function first to get a list of conversations, then use enter_conversation_directly
    with the chosen conversation data.
    """
    response_model = ResponseMessage[dict]
    
    if not await check_authorization(ctx):
        return response_model(error="User is not authorized.", success=False)
    
    try:
        page = await ctx.get_current_page()
        
        # Initialize the result structure
        result = {
            "messages": [],
            "metadata": {
                "total_count": 0,
                "fetch_time": datetime.now().isoformat()
            }
        }
        
        # Check if we're already on the messaging page
        current_url = page.url
        if not current_url.startswith('https://www.linkedin.com/messaging'):
            logger.info("Navigating to messaging page...")
            await page.goto('https://www.linkedin.com/messaging/')
        
        # Wait for the messaging overlay
        messaging_container = page.locator('div.msg-overlay-list-bubble')
        await messaging_container.wait_for(state="visible", timeout=10000)
        
        # Wait for the conversation list
        conversation_list = page.locator('.msg-conversations-container__conversations-list')
        await conversation_list.wait_for(state="visible", timeout=10000)
        
        # Get all conversation threads
        thread_elements = page.locator('.msg-conversation-card')
        thread_count = await thread_elements.count()
        
        result["metadata"]["total_count"] = thread_count
        logger.info(f"Found {thread_count} conversations")
        
        # Gather basic message preview information
        for i in range(thread_count):
            try:
                thread = thread_elements.nth(i)
                
                # Get name
                name_element = thread.locator('.msg-conversation-card__participant-names')
                name = await name_element.text_content()
                name = name.strip()
                
                # Get last message time
                try:
                    time_element = thread.locator('.msg-conversation-card__time-stamp')
                    last_message_time = await time_element.text_content()
                    last_message_time = last_message_time.strip()
                except:
                    last_message_time = None
                
                # Try to get message preview text
                try:
                    preview_element = thread.locator('.msg-conversation-card__message-snippet')
                    preview_text = await preview_element.text_content()
                    preview_text = preview_text.strip()
                except:
                    preview_text = None
                
                # Store only the essential preview info
                message_data = {
                    "name": name,
                    "last_message_time": last_message_time,
                    "preview": preview_text
                }
                
                result["messages"].append(message_data)
                logger.info(f"Gathered message preview for: {name}")
                
            except Exception as e:
                logger.error(f"Error gathering message preview: {str(e)}")
                continue
        
        return response_model(result=result)
        
    except Exception as e:
        logger.error(f"Error in fetch_profile_in_message: {str(e)}")
        return response_model(error=str(e), success=False)

async def enter_conversation_directly(ctx: BrowserContext, target_name: str) -> ResponseMessage[bool]:
    """
    Enters a specific conversation based on the contact name.
    
    Workflow:
    1. First use fetch_profile_in_message() to get list of conversations
    2. Find the exact conversation by matching the target_name
    3. Enter that specific conversation
    
    Args:
        ctx (BrowserContext): Browser context
        target_name (str): Exact name of the contact to chat with (must match the name from fetch_profile_in_message)
        
    Returns:
        ResponseMessage[bool]: Success/failure status with error message if failed
    """
    response_model = ResponseMessage[bool]
    
    if not await check_authorization(ctx):
        return response_model(error="User is not authorized.", success=False)
    
    try:
        # First get the list of conversations
        conversations_result = await fetch_profile_in_message(ctx)
        if not conversations_result.success:
            return response_model(error=f"Failed to fetch conversations: {conversations_result.error}", success=False)
        
        # Find the matching conversation
        target_conversation = None
        target_index = -1
        
        for idx, msg in enumerate(conversations_result.result["messages"]):
            if msg["name"].strip() == target_name.strip():
                target_conversation = msg
                target_index = idx
                break
        
        if target_conversation is None:
            return response_model(
                error=f"Could not find conversation with contact: {target_name}. Please verify the exact name from the conversation list.", 
                success=False
            )
        
        page = await ctx.get_current_page()
        
        try:
            # Ensure we're on messaging page
            messaging_container = page.locator('div.msg-overlay-list-bubble')
            if not await messaging_container.is_visible():
                logger.info("Opening messaging overlay...")
                await page.goto('https://www.linkedin.com/messaging/')
                await messaging_container.wait_for(state="visible", timeout=10000)
            
            logger.info(f"Attempting to enter conversation with: {target_name}")
            
            # Locate the conversation card
            conversation_cards = page.locator('.msg-conversation-card')
            count = await conversation_cards.count()
            
            if count == 0:
                logger.info("No conversations visible, checking alternative selectors...")
                alternative_selectors = [
                    'li.msg-conversation-listitem',
                    '.msg-conversation-listitem__link',
                    '.msg-selectable-entity'
                ]
                
                for selector in alternative_selectors:
                    conversation_cards = page.locator(selector)
                    count = await conversation_cards.count()
                    if count > 0:
                        logger.info(f"Found conversations using: {selector}")
                        break
            
            if target_index >= count:
                return response_model(
                    error=f"Conversation index {target_index} out of range (total: {count})", 
                    success=False
                )
                
            # Get the specific conversation card
            card = conversation_cards.nth(target_index)
            
            # Verify we're clicking the right conversation
            name_element = card.locator('.msg-conversation-card__participant-names')
            if await name_element.count() > 0:
                actual_name = await name_element.text_content()
                actual_name = actual_name.strip()
                if actual_name != target_name:
                    return response_model(
                        error=f"Name mismatch: Expected '{target_name}', found '{actual_name}'",
                        success=False
                    )
            
            # Click the conversation
            logger.info("Entering conversation...")
            await card.click()
            await page.wait_for_url("**/messaging/thread/**", timeout=5000)
            
            logger.info(f"Successfully entered conversation with {target_name}")
            return response_model(result=True)
            
        except Exception as e:
            logger.error(f"Failed to enter conversation: {str(e)}")
            return response_model(error=str(e), success=False)
            
    except Exception as e:
        logger.error(f"Error in enter_conversation_directly: {str(e)}")
        return response_model(error=str(e), success=False)
