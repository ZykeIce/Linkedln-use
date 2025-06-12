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


