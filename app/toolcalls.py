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
            for selector in ['.msg-conversations-container', '.msg-overlay-list-bubble']:
                try:
                    await page.wait_for_selector(selector, timeout=3000)
                    break
                except:
                    continue

        # Try to find conversations immediately
        conversations = []
        for selector in [
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

        # If no conversations found, try one quick reload
        if not conversations:
            await page.reload(wait_until='domcontentloaded')
            await page.wait_for_timeout(1000)
            for selector in [
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

        if not conversations:
            logger.warning("No conversations found")
            return response_model(result=[])

        results = []
        for i, conv in enumerate(conversations):
            if i >= limit:
                break

            try:
                # Get real conversation ID first
                conv_id = None
                
                # Method 1: Direct attribute
                try:
                    conv_id = await conv.get_attribute('data-conversation-id')
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
                
                # Method 3: From conversation card
                if not conv_id:
                    try:
                        card = await conv.query_selector('.msg-conversation-card')
                        if card:
                            card_id = await card.get_attribute('id')
                            if card_id:
                                conv_id = card_id.replace('conversation-', '')
                    except:
                        pass

                # Get participant name
                participant = None
                for selector in ['.msg-conversation-card__participant-names', '.msg-overlay-bubble-header__title']:
                    try:
                        participant = await conv.eval_on_selector(selector, "el => el.innerText")
                        if participant:
                            break
                    except:
                        continue

                # Get message preview
                preview = None
                for selector in ['.msg-conversation-card__message-snippet', '.msg-overlay-list-bubble__message-snippet']:
                    try:
                        preview = await conv.eval_on_selector(selector, "el => el.innerText")
                        if preview:
                            break
                    except:
                        continue

                if participant or preview:  # Only add if we found some content
                    results.append(LinkedInMessage(
                        conversation_id=conv_id or str(uuid.uuid4()),
                        participant_name=participant or "Unknown Contact",
                        message_preview=preview or "",
                        timestamp="Recent"
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
    page = await ctx.get_current_page()
    
    # Check if we're already in the conversation
    current_conversation = await page.query_selector(f'.msg-conversation-listitem[data-conversation-id="{thread_id}"]')
    
    if current_conversation is not None:
        await current_conversation.click()
        await page.wait_for_selector('.msg-conversation-card__message-snippet-body')
        return True

    # If not found in the current view, we might need to search or navigate
    element: str = query_cached_element(f'linkedin-thread-element-{thread_id}')
    
    if not element:
        return False

    container = await page.query_selector('.msg-conversations-container')
    
    if not container:
        await page.goto('https://www.linkedin.com/messaging/', wait_until='domcontentloaded')
        container = await page.query_selector('.msg-conversations-container')

    if not container:
        logger.error("Failed to find messages container in the page.")
        return False

    # Try to find and click the conversation
    conversation = await page.query_selector(f'.msg-conversation-listitem[data-conversation-id="{thread_id}"]')
    
    if not conversation:
        logger.error(f"Conversation with ID {thread_id} not found.")
        return False

    await conversation.click()
    await page.wait_for_selector('.msg-conversation-card__message-snippet-body')
    
    return True

# 2
async def enter_conversation(
    ctx: BrowserContext,
    conversation_id: str
) -> ResponseMessage[LinkedInConversation]:
    """Get complete messages from a conversation using LinkedIn's GraphQL API"""
    page = await ctx.get_current_page()
    response_model = ResponseMessage[LinkedInConversation]

    try:
        # Make sure we're on the main messaging page
        if not page.url.startswith('https://www.linkedin.com/messaging'):
            await page.goto('https://www.linkedin.com/messaging/', wait_until='networkidle')
            await page.wait_for_timeout(2000)

        # Get messages using LinkedIn's GraphQL API
        messages = await page.evaluate("""
            (async function getMessages(convId) {
                try {
                    // Get required tokens from cookies
                    const csrfToken = document.cookie
                        .split(';')
                        .find(c => c.trim().startsWith('JSESSIONID'))
                        ?.split('=')[1];

                    if (!csrfToken) return null;

                    // GraphQL query for messages
                    const query = {
                        query: `
                            query GetMessages($conversationId: String!) {
                                messagingThreadConnection(threadUrn: $conversationId) {
                                    elements {
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
                                        participants {
                                            name
                                        }
                                    }
                                }
                            }
                        `,
                        variables: {
                            conversationId: `urn:li:thread:${convId}`
                        }
                    };

                    // Make the API request
                    const response = await fetch('https://www.linkedin.com/voyager/api/graphql', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'csrf-token': csrfToken,
                            'x-li-track': '{"clientVersion":"1.12.123"}',
                            'x-restli-protocol-version': '2.0.0'
                        },
                        credentials: 'include',
                        body: JSON.stringify(query)
                    });

                    if (!response.ok) {
                        // If GraphQL fails, try the REST API
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
                                .filter(msg => msg.eventContent && msg.eventContent.attributedBody)
                                .map(msg => ({
                                    sender: msg.from?.miniProfile?.firstName + ' ' + msg.from?.miniProfile?.lastName || 'Unknown',
                                    text: msg.eventContent.attributedBody.text,
                                    timestamp: msg.createdAt
                                }));
                        }
                        return null;
                    }

                    const data = await response.json();
                    if (!data.data?.messagingThreadConnection?.elements?.[0]) return null;

                    const thread = data.data.messagingThreadConnection.elements[0];
                    return thread.messages.edges.map(edge => ({
                        sender: edge.node.sender.name || 'Unknown',
                        text: edge.node.text,
                        timestamp: edge.node.created
                    }));

                } catch (e) {
                    console.warn('Failed to fetch messages:', e);
                    return null;
                }
            })(arguments[0])
        """, conversation_id)

        # If API calls fail, try getting from Redux store
        if not messages:
            messages = await page.evaluate("""
            (function getMessagesFromStore(convId) {
                try {
                    const state = window.__INITIAL_STATE__;
                    if (!state?.messaging?.conversations) return null;

                    const conv = state.messaging.conversations[convId];
                    if (!conv?.events) return null;

                    return conv.events
                        .filter(event => 
                            event.eventContent && 
                            (event.eventContent.attributedBody?.text || event.eventContent.string)
                        )
                        .map(event => ({
                            sender: event.from?.miniProfile?.firstName + ' ' + event.from?.miniProfile?.lastName || 'Unknown',
                            text: event.eventContent.attributedBody?.text || event.eventContent.string,
                            timestamp: event.createdAt
                        }));
                } catch (e) {
                    console.warn('Store extraction failed:', e);
                    return null;
                }
            })(arguments[0])
            """, conversation_id)

        # Get participant info from the API response or store
        participant = await page.evaluate("""
            (async function getParticipantInfo(convId) {
                try {
                    // Try GraphQL first
                    const csrfToken = document.cookie
                        .split(';')
                        .find(c => c.trim().startsWith('JSESSIONID'))
                        ?.split('=')[1];

                    if (csrfToken) {
                        const query = {
                            query: `
                                query GetParticipant($conversationId: String!) {
                                    messagingThreadConnection(threadUrn: $conversationId) {
                                        elements {
                                            participants {
                                                name
                                            }
                                        }
                                    }
                                }
                            `,
                            variables: {
                                conversationId: `urn:li:thread:${convId}`
                            }
                        };

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

                        if (response.ok) {
                            const data = await response.json();
                            const participants = data.data?.messagingThreadConnection?.elements?.[0]?.participants;
                            if (participants?.length > 0) {
                                return participants[0].name;
                            }
                        }
                    }

                    // Fallback to store
                    const state = window.__INITIAL_STATE__;
                    if (state?.messaging?.profiles) {
                        const profiles = Object.values(state.messaging.profiles);
                        const profile = profiles.find(p => p?.entityUrn?.includes(convId));
                        if (profile?.miniProfile) {
                            return profile.miniProfile.firstName + ' ' + profile.miniProfile.lastName;
                        }
                    }

                    return null;
                } catch (e) {
                    console.warn('Failed to get participant info:', e);
                    return null;
                }
            })(arguments[0])
        """, conversation_id)

        conversation = LinkedInConversation(
            conversation_id=conversation_id,
            messages=messages or [],
            participant_name=participant or "Unknown Contact"
        )

        return response_model(result=conversation)

    except Exception as e:
        error_msg = f"Failed to read conversation: {str(e)}"
        logger.error(error_msg)
        return response_model(error=error_msg, success=False)

async def compose_regions(ctx: BrowserContext) -> list[ElementHandle]:
    page = await ctx.get_current_page()
    regions = await page.query_selector_all('div[role="region"][data-compose-id]') 
    return regions

async def send_key_combo(element: ElementHandle, keys: list[str]) -> bool:
    """
    Send a key combination to the specified element.
    """
    
    if not element:
        logger.error("Element is None, cannot send key combination.")
        return False

    await element.focus()
    
    for key in keys:
        await element.keyboard.down(key)

    await asyncio.sleep(0.1)  # Small delay to ensure the keys are registered

    for key in reversed(keys):
        await element.keyboard.up(key)

async def discard_drafts(ctx: BrowserContext) -> bool:
    await ensure_authorized(ctx)
    regions = await compose_regions(ctx)

    for region in regions:
        logger.info(f"Discarding draft in region: {region}")
        await send_key_combo(region, ['Control', 'Shift', 'd'])  # Ctrl + Shift + D to discard draft

# 6
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
                "name": "enter_conversation",
                "description": "Open a specific LinkedIn conversation by its ID and get the full message history.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {
                            "type": "string",
                            "description": "The ID of the conversation to open."
                        }
                    },
                    "required": ["conversation_id"],
                    "additionalProperties": False
                },
                "strict": False
            }
        },
        {
            "type": "function",
            "function": {
                "name": "send_message",
                "description": "Send a new message in a LinkedIn conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {
                            "type": "string",
                            "description": "The ID of the conversation to send the message in."
                        },
                        "message": {
                            "type": "string",
                            "description": "The message text to send."
                        }
                    },
                    "required": ["conversation_id", "message"],
                    "additionalProperties": False
                },
                "strict": False
            }
        },
        {
            "type": "function",
            "function": {
                "name": "start_new_conversation",
                "description": "Start a new LinkedIn conversation with a connection.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recipient": {
                            "type": "string",
                            "description": "The name or profile URL of the LinkedIn connection to message."
                        },
                        "message": {
                            "type": "string",
                            "description": "The initial message to send."
                        }
                    },
                    "required": ["recipient", "message"],
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
    elif tool_name == "enter_conversation":
        conversation_id = args.pop('conversation_id', None)
        if not conversation_id:
            return response_model(error="Conversation ID is required", success=False)
        return await enter_conversation(ctx, conversation_id)
    elif tool_name == "send_message":
        conversation_id = args.pop('conversation_id', None)
        message = args.pop('message', None)
        if not conversation_id or not message:
            return response_model(error="Both conversation_id and message are required", success=False)
        return await reply_to_thread(ctx, thread_id=conversation_id, message=message)
    elif tool_name == "start_new_conversation":
        recipient = args.pop('recipient', None)
        message = args.pop('message', None)
        if not recipient or not message:
            return response_model(error="Both recipient and message are required", success=False)
        return await compose_email(ctx, recipient=recipient, subject="", body=message)
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
        
        