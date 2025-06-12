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
        allowed_unauthorized = ['sign_out', 'check_login_status', 'read_full_conversation']
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

async def read_full_conversation(ctx: BrowserContext) -> ResponseMessage[list[str]]:
    """
    Reads all messages in the currently open LinkedIn conversation thread.
    """
    response_model = ResponseMessage[list[str]]
    
    if not await check_authorization(ctx):
        return response_model(error="User is not authorized.", success=False)
    
    try:
        page = await ctx.get_current_page()
        
        # 1) Wait until at least one bubble appears
        await page.wait_for_selector(
            "div.msg-s-event-listitem__message-bubble p",
            timeout=15_000
        )

        # 2) Grab all <p> under those bubble divs
        elems = await page.query_selector_all(
            "div.msg-s-event-listitem__message-bubble p"
        )

        # 3) Extract their text
        messages = []
        for el in elems:
            txt = (await el.inner_text()).strip()
            if txt:
                messages.append(txt)

        return response_model(result=messages)
            
    except PlaywrightTimeoutError:
        return response_model(error="Timeout waiting for messages to load", success=False)
    except Exception as e:
        logger.error(f"Error reading conversation: {str(e)}")
        return response_model(error=f"Failed to read conversation: {str(e)}", success=False)

