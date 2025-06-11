from browser_use.browser.context import BrowserContext
from playwright.async_api._generated import ElementHandle
from typing import Literal, Optional, TypedDict, Generic, TypeVar, Any
from .controllers import (
    check_authorization,
    ensure_url,
    get_login_status
)
from .signals import (
    UnauthorizedAccess,
    RequireUserConfirmation
)
from pydantic import BaseModel, model_validator
from datetime import datetime, timedelta
from functools import lru_cache
import pickle
import os
import logging
import httpx
from bs4 import BeautifulSoup
import asyncio
import re
import uuid

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

class LinkedInMessage(BaseModel):
    conversation_id: str
    participant_name: str
    message_preview: str
    timestamp: str
    has_attachment: bool = False


    
cache_dir = os.path.join('/storage', 'cache')
os.makedirs(cache_dir, exist_ok=True)

@lru_cache(maxsize=256)
def query_cached_element(key: str) -> Optional[Any]:
    """Retrieve a cached element handle by its ID."""
    obj_path = os.path.join(cache_dir, key)
    if not os.path.exists(obj_path):
        return None
    with open(obj_path, 'rb') as f:
        element = pickle.load(f)
    return element

def cache_element(key: str, element: Any) -> bool:
    """Cache the element handle for later use."""
    if not key:
        return False
    obj_path = os.path.join(cache_dir, key)
    if os.path.exists(obj_path):
        return True
    with open(obj_path, 'wb') as f:
        pickle.dump(element, f)
    return True

def has_cached_element(key: str) -> bool:
    """Check if an element is cached by its ID."""
    obj_path = os.path.join(cache_dir, key)
    return os.path.exists(obj_path)

async def wait_for_page_load(page, selector: str, timeout: int = 5000) -> bool:
    """Helper function to wait for page load with better error handling"""
    try:
        # Only wait for networkidle for a short time
        await page.wait_for_load_state('networkidle', timeout=3000)
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception as e:
        logger.warning(f"Page load timeout: {e}")
        return False

async def get_current_conversations(
    ctx: BrowserContext,
    silent: bool = False,
    limit: int = 30,
    visibility: bool = False,
    include_words: Optional[str] = None
) -> ResponseMessage[list[LinkedInMessage]]:
    """Get all conversations currently shown on the screen"""
    page = await ctx.get_current_page()
    response_model = ResponseMessage[list[LinkedInMessage]]

    try:
        # Make sure we're on the messaging page and handle loading properly
        if not page.url.startswith('https://www.linkedin.com/messaging'):
            logger.info("Navigating to messaging page...")
            await page.goto('https://www.linkedin.com/messaging/', wait_until='domcontentloaded')
            
            # Try to find any messaging related element with shorter timeouts
            for selector in [
                'div[data-test-id="messaging-thread-list"]',  # Latest 2024 selector
                '.msg-conversations-container__conversations-list',
                '.msg-conversations-container',
                '.msg-overlay-list-bubble'
            ]:
                try:
                    await page.wait_for_selector(selector, timeout=3000)
                    break
                except:
                    continue

        # Try to find conversations with latest selectors
        conversations = []
        for selector in [
            'div[data-test-id="messaging-thread"]',  # Latest 2024 selector
            '.msg-conversation-card__message',
            'div.msg-conversation-listitem',
            '.msg-conversation-card',
            '.msg-overlay-list-bubble__convo-item'
        ]:
            try:
                conversations = await page.query_selector_all(selector)
                if conversations:
                    break
            except:
                continue

        # If no conversations found, try GraphQL API
        if not conversations:
            try:
                # Get CSRF token
                csrf_token = await page.evaluate("""
                    document.cookie
                        .split(';')
                        .find(c => c.trim().startsWith('JSESSIONID'))
                        ?.split('=')[1]
                """)
                
                if csrf_token:
                    # Try GraphQL API
                    conversations_data = await page.evaluate("""
                        async (token) => {
                            try {
                                const response = await fetch('https://www.linkedin.com/voyager/api/messaging/conversations', {
                                    headers: {
                                        'accept': 'application/vnd.linkedin.normalized+json+2.1',
                                        'csrf-token': token,
                                        'x-restli-protocol-version': '2.0.0'
                                    },
                                    credentials: 'include'
                                });
                                
                                if (response.ok) {
                                    const data = await response.json();
                                    return data.elements;
                                }
                            } catch (e) {
                                console.error('API error:', e);
                            }
                            return null;
                        }
                    """, csrf_token)
                    
                    if conversations_data:
                        return response_model(result=[
                            LinkedInMessage(
                                conversation_id=conv.get('entityUrn', str(uuid.uuid4())),
                                participant_name=conv.get('participants', [{}])[0].get('name', 'Unknown Contact'),
                                message_preview=conv.get('previewText', ''),
                                timestamp=conv.get('lastActivityAt', 'Recent')
                            ) for conv in conversations_data[:limit]
                        ])

            except Exception as api_error:
                logger.warning(f"GraphQL API attempt failed: {api_error}")

            # If still no conversations, try one quick reload
            await page.reload(wait_until='domcontentloaded')
            await page.wait_for_timeout(1000)
            for selector in [
                'div[data-test-id="messaging-thread"]',  # Latest 2024 selector
                '.msg-conversation-card__message',
                'div.msg-conversation-listitem',
                '.msg-conversation-card'
            ]:
                try:
                    conversations = await page.query_selector_all(selector)
                    if conversations:
                        break
                except:
                    continue

        if not conversations:
            logger.warning("No conversations found")
            return response_model(result=[])

        results = []
        for i, conv in enumerate(conversations):
            if i >= limit:
                break

            try:
                # Get conversation ID using latest selectors
                conv_id = None
                
                # Method 1: Direct attribute
                try:
                    conv_id = await conv.get_attribute('data-conversation-id') or \
                             await conv.get_attribute('data-thread-id') or \
                             await conv.get_attribute('data-test-conversation-id')  # Latest 2024 attribute
                except:
                    pass
                    
                # Method 2: From URL in href
                if not conv_id:
                    try:
                        link = await conv.query_selector('a[href*="/messaging/thread/"]')
                        if link:
                            href = await link.get_attribute('href')
                            conv_id = href.split('/thread/')[-1].split('?')[0]
                    except:
                        pass

                # Get participant name using latest selectors
                participant = None
                for selector in [
                    '[data-test-id="thread-participant-name"]',  # Latest 2024 selector
                    '.msg-conversation-card__participant-names',
                    '.msg-conversation-listitem__participant-names',
                    '.msg-overlay-bubble-header__title'
                ]:
                    try:
                        participant = await conv.eval_on_selector(selector, "el => el.innerText")
                        if participant:
                            break
                    except:
                        continue

                # Get message preview using latest selectors
                preview = None
                for selector in [
                    '[data-test-id="thread-preview-text"]',  # Latest 2024 selector
                    '.msg-conversation-card__message-snippet',
                    '.msg-conversation-listitem__message-snippet',
                    '.msg-overlay-list-bubble__message-snippet'
                ]:
                    try:
                        preview = await conv.eval_on_selector(selector, "el => el.innerText")
                        if preview:
                            break
                    except:
                        continue

                # Get timestamp using latest selectors
                timestamp = "Recent"
                for selector in [
                    '[data-test-id="thread-timestamp"]',  # Latest 2024 selector
                    '.msg-conversation-card__timestamp',
                    '.msg-conversation-listitem__timestamp'
                ]:
                    try:
                        timestamp = await conv.eval_on_selector(selector, "el => el.innerText") or "Recent"
                        break
                    except:
                        continue

                if participant or preview:
                    results.append(LinkedInMessage(
                        conversation_id=conv_id or str(uuid.uuid4()),
                        participant_name=participant or "Unknown Contact",
                        message_preview=preview or "",
                        timestamp=timestamp,
                        has_attachment=bool(await conv.query_selector('.msg-conversation-card__attachment-icon'))
                    ))
            except Exception as e:
                logger.warning(f"Failed to process conversation {i}: {e}")
                continue

        return response_model(result=results)

    except Exception as e:
        error_msg = f"Failed to get conversations: {str(e)}"
        logger.error(error_msg)
        return response_model(error=error_msg, success=False)

async def ensure_authorized(ctx: BrowserContext) -> bool:
    if not await check_authorization(ctx):
        await ensure_url(ctx, 'https://www.linkedin.com/')
        raise UnauthorizedAccess('You are not authorized to access this resource. Please log in to your LinkedIn account.')

    await ensure_url(ctx, 'https://www.linkedin.com/')
    return True

async def craft_query(
    from_date: Optional[str]=None, 
    to_date: Optional[str]=None,
    sender: Optional[str]=None, 
    recipient: Optional[str]=None,
    include_words: Optional[str]="", 
    has_attachment: Optional[bool]=False,
    section: Literal["inbox", "sent", "spam", "trash", "starred"] = "inbox"
):
    query_str = f'{include_words}' if include_words else ''

    if from_date:
        query_str += f' after:{from_date}'

    if to_date:
        if to_date == from_date:
            to_date_obj = datetime.strptime(to_date, '%Y/%m/%d')
            correct_to_date = to_date_obj + timedelta(days=1)
            to_date = correct_to_date.strftime('%Y/%m/%d')

        query_str += f' before:{to_date}'

    if sender:
        query_str += f' from:{sender}'

    if recipient:
        query_str += f' to:{recipient}'

    if has_attachment:
        query_str += ' has:attachment'

    if section != "inbox":
        if section == 'starred':
            query_str += ' is:starred'
        else:
            query_str += f' in:{section}'

    return query_str.strip()

# 1
async def list_threads(
    ctx: BrowserContext, 
    from_date: Optional[str]=None, 
    to_date: Optional[str]=None,
    sender: Optional[str]=None, 
    recipient: Optional[str]=None,
    include_words: Optional[str]="", 
    has_attachment: Optional[bool]=False,
    section: Literal["inbox", "sent", "spam", "trash"] = "inbox",
    limit: int = 30
) -> ResponseMessage[list[LinkedInMessage]]:
    await ensure_authorized(ctx)

    query = await craft_query(
        from_date=from_date, 
        to_date=to_date,
        sender=sender, 
        recipient=recipient,
        include_words=include_words, 
        has_attachment=has_attachment,
        section=section
    )

    page = await ctx.get_current_page()
    dest = f'https://www.linkedin.com/messaging/'
    await page.goto(dest, wait_until='domcontentloaded')

    if query:
        await search_email(ctx, query)

    return await get_current_conversations(ctx, silent=False, limit=limit)


    


async def sign_out(ctx: BrowserContext) -> ResponseMessage[bool]:
    response_model = ResponseMessage[bool]

    if not await check_authorization(ctx):
        return response_model(result=True)

    page = await ctx.get_current_page()
    await page.goto('https://www.linkedin.com/')

    for file in os.listdir(cache_dir):
        file_path = os.path.join(cache_dir, file)

        if os.path.isfile(file_path):
            os.unlink(file_path)

    query_cached_element.cache_clear()
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
            "type": "function",
            "function": {
                "name": "list_conversations",
                "description": "List LinkedIn conversations/messages that are currently visible.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_words": {
                            "type": ["string", "null"],
                            "description": "Include words to filter conversations."
                        },
                        "limit": {
                            "type": "number",
                            "default": 30,
                            "description": "The maximum number of conversations to return."
                        }
                    },
                    "required": [],
                    "additionalProperties": False
                },
                "strict": False
            }
        },

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
        }
    ]

    is_authorized = await check_authorization(ctx)

    if is_authorized:
        # Return all tools except sign_out when authorized
        return [tool for tool in toolcalls if tool['function']['name'] != 'sign_out']
    else:
        # Return only sign_out and check_login_status when not authorized
        return [tool for tool in toolcalls if tool['function']['name'] in ['sign_out', 'check_login_status']]

async def execute_toolcall(
    ctx: BrowserContext, 
    tool_name: str, 
    args: dict[str, Any]
) -> ResponseMessage[Any]:
    response_model = ResponseMessage[Any]

    if tool_name == "check_login_status":
        return await check_login_status(ctx)
    elif tool_name == "list_conversations":
        return await get_current_conversations(ctx, **args)
    elif tool_name == "sign_out":
        return await sign_out(ctx)

    else:
        return response_model(error=f"Unknown tool call: {tool_name}", success=False)

async def get_current_user_identity(
    ctx: BrowserContext
) -> ResponseMessage[str]:
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

