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

class LinkedInConversation(BaseModel):
    conversation_id: str
    messages: list[dict]  # Each message will have text, sender, timestamp
    participant_name: str
    
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

async def extract_mail_message_detail(ctx: BrowserContext, id: str) -> Optional[LinkedInMessage]:
    if has_cached_element(f'mail-message-{id}'):
        return query_cached_element(f'mail-message-{id}')

    page = await ctx.get_current_page()
    ik = await page.evaluate(
        """() => {
            const gmIdKey = Object.keys(window).find(key => key.startsWith('GM_ID_KEY'));
            return window[gmIdKey] || '';
        }"""
    )
    
    cookies_list = await ctx.browser_context.cookies(
        urls=['https://www.linkedin.com']
    )
    
    cookies = {cookie['name']: cookie['value'] for cookie in cookies_list}

    params = {
        'ik': ik,
        'view': 'om',
        'permmsgid': id
    }

    url = f'https://www.linkedin.com/messaging/'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url, 
            params=params, 
            cookies=cookies, 
            headers=headers
        )

    if response.status_code != 200:
        logger.error(f"Failed to fetch mail thread {id}: {response.status_code} {response.text}")
        return None

    soup = BeautifulSoup(response.text, features="html.parser")
    pre = soup.find('pre', {'id': 'raw_message_text'})

    if not pre:
        logger.error(f"Failed to find raw message text for thread {id}.")
        return None
    
    raw = pre.text

    msg = BytesParser(policy=policy.default).parsebytes(raw.encode())
    body = msg.get_body(preferencelist=('plain')).get_content() if msg.get_body(preferencelist=('plain')) else ""
    datetime_obj = parsedate_to_datetime(msg['date']) if msg['date'] else None
    timestamp = int(datetime_obj.timestamp()) if datetime_obj else 0

    detail = LinkedInMessage(
        conversation_id=id,
        participant_name=msg['from'] or "Unknown Sender",
        message_preview=body or "",
        timestamp=str(msg['date'] or "Unknown Date"),
        has_attachment=False
    )

    if not cache_element(f'mail-message-{id}', detail):
        logger.warning(f"Failed to cache mail message with ID: {id}")

    return detail
    
async def ensure_thread_opened(ctx: BrowserContext, thread_id: str) -> bool:
    """Ensure a specific conversation thread is opened"""
    page = await ctx.get_current_page()
    
    # Check if we're already in the conversation using latest selectors
    current_conversation = await page.query_selector(f'[data-test-conversation-id="{thread_id}"]') or \
                         await page.query_selector(f'[data-conversation-id="{thread_id}"]') or \
                         await page.query_selector(f'[data-thread-id="{thread_id}"]')
    
    if current_conversation is not None:
        try:
            await current_conversation.click()
            # Wait for message container with latest selector
            await page.wait_for_selector('[data-test-id="message-container"]', timeout=5000)
            return True
        except Exception as e:
            logger.warning(f"Failed to click conversation: {e}")
            return False

    # If not found in the current view, try to find it
    try:
        # Make sure we're on messaging page
        if not page.url.startswith('https://www.linkedin.com/messaging'):
            await page.goto('https://www.linkedin.com/messaging/', wait_until='domcontentloaded')
            await page.wait_for_timeout(2000)

        # Try to find the conversation with latest selectors
        conversation = None
        for selector in [
            f'[data-test-conversation-id="{thread_id}"]',
            f'[data-conversation-id="{thread_id}"]',
            f'[data-thread-id="{thread_id}"]',
            f'a[href*="/messaging/thread/{thread_id}"]'
        ]:
            conversation = await page.query_selector(selector)
            if conversation:
                break

        if not conversation:
            # Try searching for the conversation
            search_box = await page.query_selector('[data-test-id="messaging-search-box"]') or \
                        await page.query_selector('.msg-search-box__search-box')
            
            if search_box:
                await search_box.click()
                await search_box.type(thread_id)
                await page.wait_for_timeout(1000)
                
                # Try to find the conversation again after search
                for selector in [
                    f'[data-test-conversation-id="{thread_id}"]',
                    f'[data-conversation-id="{thread_id}"]',
                    f'[data-thread-id="{thread_id}"]',
                    f'a[href*="/messaging/thread/{thread_id}"]'
                ]:
                    conversation = await page.query_selector(selector)
                    if conversation:
                        break

        if not conversation:
            logger.error(f"Conversation with ID {thread_id} not found.")
            return False

        # Click the conversation and wait for it to load
        await conversation.click()
        await page.wait_for_selector('[data-test-id="message-container"]', timeout=5000)
        return True

    except Exception as e:
        logger.error(f"Failed to open thread {thread_id}: {e}")
        return False

async def read_direct_messages(
    ctx: BrowserContext,
    conversation_id: str
) -> ResponseMessage[list[dict]]:
    """Read direct messages from a specific LinkedIn conversation"""
    response_model = ResponseMessage[list[dict]]
    page = await ctx.get_current_page()

    try:
        # Make sure we're on the messaging page
        if not page.url.startswith('https://www.linkedin.com/messaging'):
            await page.goto('https://www.linkedin.com/messaging/', wait_until='domcontentloaded')
            await page.wait_for_timeout(2000)

        # Try to open the conversation
        if not await ensure_thread_opened(ctx, conversation_id):
            return response_model(error="Failed to open conversation", success=False)

        # Get messages using LinkedIn's GraphQL API
        messages = await page.evaluate("""
            async function getMessages(convId) {
                try {
                    // Get CSRF token from cookies
                    const csrfToken = document.cookie
                        .split(';')
                        .find(c => c.trim().startsWith('JSESSIONID'))
                        ?.split('=')[1];

                    if (!csrfToken) return null;

                    // Try REST API first
                    const restResponse = await fetch(`https://www.linkedin.com/voyager/api/messaging/conversations/${convId}/events`, {
                        headers: {
                            'accept': 'application/vnd.linkedin.normalized+json+2.1',
                            'csrf-token': csrfToken,
                            'x-restli-protocol-version': '2.0.0'
                        },
                        credentials: 'include'
                    });

                    if (restResponse.ok) {
                        const data = await restResponse.json();
                        if (!data.elements) return null;

                        return data.elements
                            .filter(msg => msg.eventContent && (msg.eventContent.attributedBody || msg.eventContent.text))
                            .map(msg => ({
                                sender: msg.from?.miniProfile?.firstName 
                                    ? `${msg.from.miniProfile.firstName} ${msg.from.miniProfile.lastName || ''}`
                                    : 'Unknown',
                                text: msg.eventContent.attributedBody?.text || msg.eventContent.text || '',
                                timestamp: msg.createdAt
                            }));
                    }

                    return null;
                } catch (e) {
                    console.error('Failed to fetch messages:', e);
                    return null;
                }
            }
            return await getMessages(arguments[0]);
        """, conversation_id)

        if not messages:
            # If API call fails, try scraping the UI
            try:
                message_elements = await page.query_selector_all('[data-test-id="message-container"] [data-test-id="message-body"]')
                sender_elements = await page.query_selector_all('[data-test-id="message-container"] [data-test-id="message-sender-name"]')
                timestamp_elements = await page.query_selector_all('[data-test-id="message-container"] [data-test-id="message-timestamp"]')
                
                scraped_messages = []
                for i in range(len(message_elements)):
                    try:
                        text = await message_elements[i].inner_text()
                        sender = await sender_elements[i].inner_text() if i < len(sender_elements) else 'Unknown'
                        timestamp = await timestamp_elements[i].inner_text() if i < len(timestamp_elements) else 'Unknown'
                        
                        scraped_messages.append({
                            'sender': sender,
                            'text': text,
                            'timestamp': timestamp
                        })
                    except Exception as e:
                        logger.warning(f"Failed to scrape message {i}: {e}")
                        continue
                
                if scraped_messages:
                    messages = scraped_messages

            except Exception as e:
                logger.warning(f"Failed to scrape messages: {e}")

        if not messages:
            return response_model(error="Failed to fetch messages", success=False)

        return response_model(result=messages)

    except Exception as e:
        error_msg = f"Failed to read messages: {str(e)}"
        logger.error(error_msg)
        return response_model(error=error_msg, success=False)

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
            "type": "function",
            "function": {
                "name": "extract_linkedin_info",
                "description": "Extract LinkedIn information in a format that's easy for AI agents to read.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_words": {
                            "type": ["string", "null"],
                            "description": "Optional words to filter conversations by."
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
    elif tool_name == "extract_linkedin_info":
        include_words = args.get('include_words', None)
        return await extract_linkedin_info(ctx, include_words)
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

async def read_linkedin_conversations(ctx: BrowserContext) -> ResponseMessage[list[LinkedInConversation]]:
    """Read LinkedIn conversations using GraphQL API"""
    page = await ctx.get_current_page()
    response_model = ResponseMessage[list[LinkedInConversation]]

    try:
        # Ensure we're on messaging page
        if not page.url.startswith('https://www.linkedin.com/messaging'):
            await page.goto('https://www.linkedin.com/messaging/', wait_until='networkidle')
            await page.wait_for_timeout(2000)

        # Get conversations using LinkedIn's GraphQL API
        conversations = await page.evaluate("""
            async function getConversations() {
                try {
                    // Get CSRF token from cookies
                    const csrfToken = document.cookie
                        .split(';')
                        .find(c => c.trim().startsWith('JSESSIONID'))
                        ?.split('=')[1];

                    if (!csrfToken) return null;

                    // GraphQL query for conversations
                    const query = {
                        query: `
                            query GetConversations {
                                messagingThreads {
                                    elements {
                                        conversationId
                                        participants {
                                            name
                                        }
                                        messages {
                                            edges {
                                                node {
                                                    text
                                                    created
                                                    sender {
                                                        name
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        `
                    };

                    // Make API request
                    const response = await fetch('https://www.linkedin.com/voyager/api/graphql', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'csrf-token': csrfToken,
                            'x-restli-protocol-version': '2.0.0'
                        },
                        credentials: 'include',
                        body: JSON.stringify(query)
                    });

                    if (!response.ok) {
                        // Try REST API as fallback
                        const restResponse = await fetch('https://www.linkedin.com/voyager/api/messaging/conversations', {
                            headers: {
                                'accept': 'application/vnd.linkedin.normalized+json+2.1',
                                'csrf-token': csrfToken,
                                'x-restli-protocol-version': '2.0.0'
                            },
                            credentials: 'include'
                        });

                        if (restResponse.ok) {
                            const data = await restResponse.json();
                            return data.elements.map(conv => ({
                                conversationId: conv.entityUrn,
                                participantName: conv.participants?.[0]?.name || 'Unknown',
                                messages: conv.events
                                    .filter(msg => msg.eventContent && msg.eventContent.attributedBody)
                                    .map(msg => ({
                                        sender: msg.from?.miniProfile?.firstName + ' ' + msg.from?.miniProfile?.lastName || 'Unknown',
                                        text: msg.eventContent.attributedBody.text,
                                        timestamp: msg.createdAt
                                    }))
                            }));
                        }
                        return null;
                    }

                    const data = await response.json();
                    return data.data?.messagingThreads?.elements?.map(thread => ({
                        conversationId: thread.conversationId,
                        participantName: thread.participants[0]?.name || 'Unknown',
                        messages: thread.messages.edges.map(edge => ({
                            sender: edge.node.sender.name || 'Unknown',
                            text: edge.node.text,
                            timestamp: edge.node.created
                        }))
                    })) || [];

                } catch (e) {
                    console.error('Failed to fetch conversations:', e);
                    return null;
                }
            }
            return await getConversations();
        """)

        if not conversations:
            return response_model(error="Failed to fetch conversations", success=False)

        return response_model(result=[
            LinkedInConversation(
                conversation_id=conv['conversationId'],
                participant_name=conv['participantName'],
                messages=conv['messages']
            ) for conv in conversations
        ])

    except Exception as e:
        error_msg = f"Failed to read conversations: {str(e)}"
        logger.error(error_msg)
        return response_model(error=error_msg, success=False)

async def extract_linkedin_info(
    ctx: BrowserContext,
    include_words: Optional[str] = None
) -> ResponseMessage[dict]:
    """Extract LinkedIn information in a format that's easy for AI agents to read"""
    response_model = ResponseMessage[dict]
    
    try:
        # First check login status
        login_status = await check_login_status(ctx)
        if not login_status.success or not login_status.result.get('is_logged_in'):
            return response_model(error="Not logged in to LinkedIn", success=False)

        # Get conversations
        conversations = await get_current_conversations(ctx, include_words=include_words)
        if not conversations.success:
            return response_model(error="Failed to get conversations", success=False)

        # Format the data in an AI-friendly way
        formatted_data = {
            "profile": login_status.result,
            "conversations": [
                {
                    "id": conv.conversation_id,
                    "participant": conv.participant_name,
                    "last_message": conv.message_preview,
                    "timestamp": conv.timestamp
                }
                for conv in conversations.result
            ]
        }

        return response_model(result=formatted_data)

    except Exception as e:
        error_msg = f"Failed to extract LinkedIn info: {str(e)}"
        logger.error(error_msg)
        return response_model(error=error_msg, success=False)
        
        