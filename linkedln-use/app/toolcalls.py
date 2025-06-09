from browser_use.browser.context import BrowserContext
from playwright.async_api._generated import ElementHandle
from typing import Literal, Optional, TypedDict, Generic, TypeVar, Any
from .controllers import (
    check_authorization,
    ensure_url,
    search_email,
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
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import asyncio
import re

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

class SimpleEMail(BaseModel):
    thread_id: str
    subject: str
    sender: str
    date: str
    snippet: str
    has_attachment: bool

    timestamp: int = 0

class EMail(BaseModel):
    message_id: str
    subject: str
    sender: str
    date: str
    body: str

    timestamp: int = 0

class EMailThread(BaseModel):
    thread_id: str
    mails: list[EMail]
    
cache_dir = os.path.join('/storage', 'cache')
os.makedirs(cache_dir, exist_ok=True)

@lru_cache(maxsize=256)
def query_cached_element(key: str) -> Optional[Any]:
    """
    Retrieve a cached element handle by its ID.
    This is a placeholder function as caching in Playwright is not straightforward.
    """

    obj_path = os.path.join(cache_dir, key)

    if not os.path.exists(obj_path):
        return None

    with open(obj_path, 'rb') as f:
        element = pickle.load(f)

    return element

def cache_element(key: str, element: Any) -> bool:
    """
    Cache the element handle for later use.
    This is a placeholder function as caching in Playwright is not straightforward.
    """
    
    if not key:
        return False

    obj_path = os.path.join(cache_dir, key)
    if os.path.exists(obj_path):
        return True

    with open(obj_path, 'wb') as f:
        pickle.dump(element, f)

    return True

def has_cached_element(key: str) -> bool:
    """
    Check if an element is cached by its ID.
    This is a placeholder function as caching in Playwright is not straightforward.
    """
    
    obj_path = os.path.join(cache_dir, key)
    return os.path.exists(obj_path)


# 0: get all mails that are currently shown in the screen
async def get_current_threads(
    ctx: BrowserContext,
    silent: bool = False,
    limit: int = 30,
    visibility: bool = False
) -> ResponseMessage[list[SimpleEMail]]:
    page = await ctx.get_current_page()
    response_model = ResponseMessage[list[SimpleEMail]]

    if not page.url.startswith('https://www.linkedin.com') and silent:
        return response_model(result=[])

    await ensure_authorized(ctx)
    await page.wait_for_load_state(state='domcontentloaded')
    
    try:
        await page.wait_for_selector('.msg-conversations-container', timeout=5000)  # Wait for the messages container
        await page.wait_for_selector('.msg-conversation-listitem', timeout=5000)  # Wait for message items
    except Exception as e:
        logger.warning(f"No messages found: {e} (this is normal if the inbox is empty)")
        return response_model(result=[])

    threads = []
    retries = 5

    while threads == [] and retries > 0:
        await page.wait_for_timeout(1000)
        threads = await page.query_selector_all('.msg-conversation-listitem')
        retries -= 1

    results = []

    for i, thread in enumerate(threads):
        if i >= limit:
            break

        if visibility:
            is_visible = await thread.is_visible()
            if not is_visible:
                logger.debug(f"Thread {i} is not visible, skipping.")
                continue

        try:
            participant = await thread.eval_on_selector(".msg-conversation-card__participant-names", "el => el.innerText")
            snippet = await thread.eval_on_selector(".msg-conversation-card__message-snippet", "el => el.innerText")
            timestamp = await thread.eval_on_selector(".msg-conversation-card__time-stamp", "el => el.innerText")
            thread_id = await thread.get_attribute('data-conversation-id')

            thread_outter_html = (await thread.get_property('outerHTML')).__str__()
            if not thread_id or not cache_element(f'linkedin-thread-element-{thread_id}', thread_outter_html):
                logger.warning(f"Failed to cache thread element with ID: {thread_id}")

            results.append(SimpleEMail(
                thread_id=thread_id or str(i),
                subject=f"Conversation with {participant}",
                sender=participant or "Unknown Contact",
                date=timestamp or "Unknown Date",
                snippet=snippet or "",
                has_attachment=False
            ))
        except Exception as e:
            logger.warning(f"Failed to process thread {i}: {e}")
            continue

    return response_model(result=results)

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
) -> ResponseMessage[list[SimpleEMail]]:
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

    return await get_current_threads(ctx, silent=False, limit=limit)

async def extract_mail_message_detail(ctx: BrowserContext, id: str) -> Optional[EMail]:
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

    detail = EMail(
        message_id=id,
        subject=msg['subject'] or "No Subject",
        sender=msg['from'] or "Unknown Sender",
        date=str(msg['date'] or "Unknown Date"),
        body=body,
        timestamp=timestamp
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
async def enter_thread(ctx: BrowserContext, thread_id: str) -> ResponseMessage[EMailThread]:
    await ensure_authorized(ctx)
    response_model = ResponseMessage[EMailThread]

    if not thread_id:
        return response_model(error="Thread ID cannot be empty or n/a.", success=False)


    page = await ctx.get_current_page() 
    await ensure_thread_opened(ctx, thread_id)

    if has_cached_element(f'mail-thread-{thread_id}'):
        return response_model(result=query_cached_element(f'mail-thread-{thread_id}'))

    mail_list = []

    # find all divs with data-message-id=*
    mail_elements = await page.query_selector_all(f'div[data-message-id]')

    for element in mail_elements:
        data_message_id = await element.get_attribute('data-message-id')
        print(f"Processing mail element with data-message-id: {data_message_id}")

        if not data_message_id:
            continue

        data_message_id = data_message_id.strip('#')
        email = await extract_mail_message_detail(ctx, data_message_id)

        if email:
            mail_list.append(email)

    direct_url = 'https://www.linkedin.com/messaging/' + page.url.split('/')[-1]
    cache_element(f'direct-url-{thread_id}', direct_url)

    mail_thread = EMailThread(
        thread_id=thread_id,
        mails=mail_list
    )
    cache_element(f'mail-thread-{thread_id}', mail_thread)

    return response_model(result=mail_thread)

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
    
# 3
async def forward_thread(
    ctx: BrowserContext,
    thread_id: str,
    recipient: str
) -> ResponseMessage[str]:
    await ensure_authorized(ctx)
    response_model = ResponseMessage[str]

    if not thread_id:
        return response_model(error="Thread ID cannot be empty or n/a.", success=False)

    if not await ensure_thread_opened(ctx, thread_id):
        return response_model(error="Thread not found or not opened.", success=False)
    
    page = await ctx.get_current_page()

    # find span with text "Forward" inside
    forward_btn = await page.query_selector('//span[normalize-space()="Forward" and @role="link"]')  # The forward button in the thread
    if not forward_btn:
        return response_model(error="Failed to find the forward button in the thread.", success=False) 

    await forward_btn.click()  # Click the forward button
    await page.wait_for_timeout(1000)  # Wait for the forward dialog to open

    regions = await compose_regions(ctx)  # Get all compose regions 

    if not regions:
        return response_model(error="No compose regions found.", success=False)

    regions_w_id = [
        (await region.get_attribute('data-compose-id') or -1, region) 
        for region in regions
    ]  
    regions_w_id.sort(key=lambda x: int(x[0]), reverse=True)  # Sort by data-compose-id in reverse order
    regions = [region for _, region in regions_w_id]  # Extract the regions from the sorted list

    if not regions:
        return response_model(error="No compose regions found.", success=False)

    if len(regions) > 1:
        logger.warning(f"Multiple compose regions found: {len(regions)}. Using the last one.")

    region = regions[0]  # Use the first compose region
    await region.focus()  # Focus on the compose region

    compose_id = await region.get_attribute('data-compose-id')  # Get the compose ID

    await page.fill(f'div[role="region"][data-compose-id="{compose_id}"] input[aria-label="To recipients"]', recipient)  # Fill the recipient input field
    await asyncio.sleep(1)  # Wait for a second to ensure the input is filled

    send_button = await region.query_selector('div.T-I.J-J5-Ji.aoO.T-I-atl[role="button"]')  # The send button
    await send_button.click()  # Click the send button
    
    await asyncio.sleep(2)  # Wait for the email to be sent
    return response_model(result="Email forwarded successfully.")

# 4
async def reply_to_thread(
    ctx: BrowserContext, 
    thread_id, 
    message: str
) -> ResponseMessage[str]:
    await ensure_authorized(ctx)
    response_model = ResponseMessage[str]

    if not thread_id:
        return response_model(error="Thread ID cannot be empty or n/a.", success=False)

    if not await ensure_thread_opened(ctx, thread_id):
        return response_model(error="Thread not found or not opened.", success=False)
    
    page = await ctx.get_current_page()

    # find span with text "Forward" inside
    forward_btn = await page.query_selector('//span[normalize-space()="Reply" and @role="link"]')  # The forward button in the thread
    if not forward_btn:
        return response_model(error="Failed to find the forward button in the thread.", success=False) 

    await forward_btn.click()  # Click the forward button
    await page.wait_for_timeout(1000)  # Wait for the forward dialog to open
    
    regions = await compose_regions(ctx)  # Get all compose regions 

    regions_w_id = [
        (await region.get_attribute('data-compose-id') or -1, region) 
        for region in regions
    ]
    regions_w_id.sort(key=lambda x: int(x[0]), reverse=True)  # Sort by data-compose-id in reverse order
    regions = [region for _, region in regions_w_id]  # Extract the regions from the sorted list
    
    if not regions:
        return response_model(error="No compose regions found.", success=False)
    
    if len(regions) > 1: 
        logger.warning(f"Multiple compose regions found: {len(regions)}. Using the first one.")

    region = regions[0]  # Use the first compose region
    await region.focus()  # Focus on the compose region

    message_input = await region.query_selector('div[aria-label="Message Body"]')  # The input field for the message body
    await message_input.fill(message)
    await asyncio.sleep(2)  # Wait for a second to ensure the input is filled

    send_button = await region.query_selector('div.T-I.J-J5-Ji.aoO.T-I-atl[role="button"]')  # The send button
    await send_button.click()  # Click the send button

    await asyncio.sleep(2)  # Wait for the email to be sent
    return response_model(result="Replied.")

# 5
async def compose_email(
    ctx: BrowserContext, 
    recipient: str, 
    subject: str, 
    body: str,
) -> ResponseMessage[str]:
    response_model = ResponseMessage[str]

    await ensure_authorized(ctx)

    page = await ctx.get_current_page()    
    compose_button = await page.query_selector('div[role="button"]:has-text("Compose")')
    await compose_button.click()
    await page.wait_for_timeout(0.5) 
    
    regions = await compose_regions(ctx)  # Get all compose regions
    
    regions_w_id = [
        (await region.get_attribute('data-compose-id') or -1, region) 
        for region in regions
    ]

    regions_w_id.sort(key=lambda x: int(x[0]), reverse=True)  # Sort by data-compose-id in reverse order
    regions = [region for _, region in regions_w_id]  # Extract the regions from the sorted list

    if not regions:
        return response_model(error="No compose regions found.", success=False)

    if len(regions) > 1:
        logger.warning(f"Multiple compose regions found: {len(regions)}. Using the first one.")

    region = regions[0]  # Use the first compose region
    await region.focus()  # Focus on the compose region

    recipient_element = await region.query_selector('input[aria-label="To recipients"]')  # The input field for the recipient

    # fill the recipient input field
    if not recipient_element:
        return response_model(
            error="Failed to find the recipient input field. User should fill this value and send it manually", 
            success=False
        )
        
    await recipient_element.fill(recipient)

    subjectbox_element = await region.query_selector('input[name="subjectbox"]')  # The input field for the subject
    await subjectbox_element.fill(subject)
    await asyncio.sleep(0.5)  # Wait for a second to ensure the input is filled

    message_body_element = await region.query_selector('div[aria-label="Message Body"]')  # The input field for the message body
    await message_body_element.fill(body)
    await asyncio.sleep(0.5)  # Wait for a second to ensure the body is filled

    compose_id = await region.get_attribute('data-compose-id')
    await asyncio.sleep(0.5)  # Wait for a second to ensure the recipient is filled

    send_button = await region.query_selector('div.T-I.J-J5-Ji.aoO.T-I-atl[role="button"]')  # The send button
    await send_button.click()  # Click the send button
    await asyncio.sleep(2)  # Wait for the email to be sent

    return response_model(result=f"Email sent (Compose ID: {compose_id})", success=True)

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

async def move_thread_to(
    ctx: BrowserContext,
    label: Literal["spam", "trash"]
):
    page = await ctx.get_current_page()

    if label == "spam":
        res = await page.evaluate(
            """() => {
                const spamButton = document.querySelector('div[aria-label="Report spam"]');
                if (spamButton) {
                    spamButton.click();
                    return true;
                } else {
                    console.error('Spam button not found.');
                }
                
                return false;
            }"""
        )  # Click the spam button using JavaScript to ensure it works even if the button is not visible

    elif label == "trash":
        res = await page.evaluate(
            """() => {
                const trashButton = document.querySelector('div[aria-label="Delete"]');
                if (trashButton) {
                    trashButton.click();
                    return true;
                } else {
                    console.error('Trash button not found.');
                }
                
                return false;
            }"""
        )

    else:
        raise ValueError(f"Invalid label: {label}. Must be 'spam' or 'trash'.")

    await page.wait_for_timeout(1000)  # Wait for a second to ensure the label is applied
    return res

# 7
async def label_threads(
    ctx: BrowserContext,
    thread_ids: str, # separated by comma
    label: Literal["spam", "trash"]
) -> ResponseMessage[str]:
    response_model = ResponseMessage[str]

    thread_ids = set([
        thread_id.strip() 
        for thread_id in thread_ids.split(',') 
        if thread_id.strip()
    ])

    not_found_ids = []
    error_ids = []
    moved = []

    for i, thread_id in enumerate(thread_ids):
        if not thread_id:
            continue

        if not await ensure_thread_opened(ctx, thread_id):
            not_found_ids.append(thread_id)
            continue
        
        try:
            if await move_thread_to(ctx, label):
                moved.append(thread_id)
            else:
                logger.error(f"Failed to move thread {thread_id} to {label}.")
                error_ids.append(thread_id)

        except Exception as e:
            logger.error(f"Failed to move thread {thread_id} to {label}: {e}")
            error_ids.append(thread_id)
            continue

    page = await ctx.get_current_page()
    current_url = await page.url
    pat = r'https://www.linkedin.com/messaging/(\d+)/#(inbox|sent|spam|trash)/'

    match = re.search(pat, current_url)

    if match:
        profile_index = match.group(1)
        section = match.group(2)
        
        if section not in ['inbox', 'sent', 'spam', 'trash']:
            section = 'inbox'

        await page.goto(f'https://www.linkedin.com/messaging/{profile_index}/#{section}/', wait_until='domcontentloaded')

    else:
        await page.goto('https://www.linkedin.com/messaging/', wait_until='domcontentloaded')

    msg = f"{len(moved)} threads moved to {label}."

    if error_ids:
        msg += f" {len(error_ids)} threads failed to move: {', '.join(error_ids)}."

    if not_found_ids:
        msg += f" {len(not_found_ids)} threads not found: {', '.join(not_found_ids)}."

    return response_model(result=msg, success=True)

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
                "name": "list_threads",
                "description": "Get email threads from LinkedIn with the specified criteria.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "from_date": {
                            "type": ["string", "null"],
                            "description": "The starting date to filter emails by, in format (yyyy/mm/dd)."
                        },
                        "to_date": {
                            "type": ["string", "null"],
                            "description": "The ending date to filter emails by, in format (yyyy/mm/dd). If not provided, it defaults to the current date."
                        },
                        "sender": {
                            "type": ["string", "null"],
                            "description": "Filter emails by sender's email address."
                        },
                        "recipient": {
                            "type": ["string", "null"],
                            "description": "Filter emails by recipient's email address."
                        },
                        "include_words": {
                            "type": ["string", "null"],
                            "description": "Include words to filter emails."
                        },
                        "has_attachment": {
                            "type": ["boolean", "null"],
                            "description": "Whether to filter emails that have attachments."
                        },
                        "section": {
                            "type": "string",
                            "enum": ["inbox", "sent", "spam", "trash"],
                            "default": "inbox",
                            "description": "The section of LinkedIn to filter emails from."
                        },
                        "limit": {
                            "type": "number",
                            "default": 30,
                            "description": "The maximum number of email threads to return."
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
                "name": "enter_thread",
                "description": "Enter a specific email thread by its ID and get full context of the thread.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "The ID of the email thread to enter."
                        }
                    },
                    "required": ["thread_id"],
                    "additionalProperties": False
                },
                "strict": False
            }
        },
        {
            "type": "function",
            "function": {
                "name": "forward_thread",
                "description": "Forward an email thread (identify by its ID) to a specified recipient.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "The ID of the email thread to forward."
                        },
                        "recipient": {
                            "type": ["string", "null"],
                            "description": "The recipient's email address to forward the thread to."
                        }
                    },
                    "required": ["thread_id", "recipient"],
                    "additionalProperties": False
                },
                "strict": False
            }
        },
        {
            "type": "function",
            "function": {
                "name": 'reply_to_thread',
                'description': 'Reply to an email (identify by its ID) thread with a message.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'thread_id': {
                            'type': 'string',
                            'description': 'The ID of the email thread to reply to.'
                        },
                        'message': {
                            'type': ['string', 'null'],
                            'description': 'The message to reply with.'
                        }
                    },
                    'required': ['thread_id', 'message'],
                    'additionalProperties': False
                },
                'strict': False
            }
        },
        {
            "type": "function",
            "function": {
                "name": "compose_email",
                "description": "Compose a new email and send it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recipient": {
                            "type": ["string", "null"],
                            "description": "The recipient's email address."
                        },
                        "subject": {
                            "type": ["string", "null"],
                            "description": "The subject of the email."
                        },
                        "body": {
                            "type": ["string", "null"],
                            "description": "The body of the email."
                        }
                    },
                    "required": ["recipient", "subject", "body"],
                    "additionalProperties": False
                },
                "strict": False
            }
        },
        { # label_threads
            'type': 'function',
            'function': {
                'name': 'label_threads',
                'description': 'Label multiple email threads with a specified label.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'thread_ids': {
                            'type': 'string',
                            'description': 'Comma-separated list of thread IDs to label.'
                        },
                        'label': {
                            'type': 'string',
                            'enum': ['spam', 'trash'],
                            'description': "The label to apply to the selected threads."
                        }
                    },
                    'required': ['thread_ids', 'label'],
                    'additionalProperties': False
                },
                'strict': False
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
    elif tool_name == "list_threads":
        return await list_threads(ctx, **args)
    elif tool_name == "enter_thread":
        return await enter_thread(ctx, **args)
    elif tool_name == "sign_out":
        return await sign_out(ctx)
    else:
        try:
            if tool_name == "forward_thread":
                return await forward_thread(ctx, **args)
            elif tool_name == "reply_to_thread":
                return await reply_to_thread(ctx, **args)
            elif tool_name == "compose_email":
                return await compose_email(ctx, **args)
            elif tool_name == "label_threads":
                return await label_threads(ctx, **args)
        except Exception as e:
            if isinstance(e, UnauthorizedAccess):
                raise e

            return response_model(error=f"Action {tool_name} failed, the user should do it manually.", success=False) 

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

    element = await page.query_selector('a.gb_B.gb_Za.gb_0')

    if not element:
        return response_model(error="Failed to find the user identity element.", success=False)

    user_identity = await element.get_attribute('aria-label')
    return response_model(result=user_identity)
        