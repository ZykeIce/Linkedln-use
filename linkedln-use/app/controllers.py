from browser_use import Controller, Browser, ActionResult
from .models import browser_use_custom_models
from browser_use.browser.context import BrowserContext
from .signals import (
    UnauthorizedAccess,
    RequireUserConfirmation
)
from typing import Literal
from fnmatch import fnmatch
import logging
from playwright._impl._api_structures import (
    ClientCertificate,
    Cookie
)
from typing import TypedDict

logger = logging.getLogger(__name__)

built_in_actions = [
    'done',
    'search_google',
    'go_to_url',
    'go_back',
    'wait',
    'click_element_by_index',
    'input_text',
    'save_pdf',
    'switch_tab',
    'open_tab',
    'close_tab',
    'extract_content',
    'scroll_down',
    'scroll_up',
    'send_keys',
    'scroll_to_text',
    'get_dropdown_options',
    'select_dropdown_option',
    'drag_drop',
    'get_sheet_contents',
    'select_cell_or_range',
    'get_range_contents',
    'clear_selected_range',
    'input_selected_cell_text',
    'update_range_contents' 
]

exclude = [
    a
    for a in built_in_actions
    if a not in [
        'done',
        # 'search_google',
        'go_to_url',
        'go_back',
        # 'wait',
        'click_element_by_index',
        'input_text',
        # 'save_pdf',
        # 'switch_tab',
        # 'open_tab',
        # 'close_tab',
        'extract_content',
        'scroll_down',
        'scroll_up',
        'send_keys',
        # 'scroll_to_text',

        'get_dropdown_options',
        'select_dropdown_option',

        # 'drag_drop',
        # 'get_sheet_contents',
        # 'select_cell_or_range',
        # 'get_range_contents',
        # 'clear_selected_range',
        # 'input_selected_cell_text',
        'update_range_contents' 
    ]
]

_controller = Controller(
    output_model=browser_use_custom_models.BasicAgentResponse,
    exclude_actions=exclude
)

async def check_authorization(ctx: BrowserContext) -> bool:
    try:
        page = await ctx.get_current_page()
        
        # First navigate to LinkedIn
        if not page.url.startswith('https://www.linkedin.com'):
            await page.goto('https://www.linkedin.com', wait_until='domcontentloaded')
        
        # Try to find elements that only appear when logged in
        try:
            # Wait a short time for the nav menu - if we're logged in, it should appear quickly
            await page.wait_for_selector('.global-nav', timeout=3000)
            
            # Check if we're redirected to login page
            current_url = page.url
            if 'login' in current_url or 'signup' in current_url:
                return False
                
            return True
            
        except Exception:
            # If we can't find nav elements or get redirected to login, we're not logged in
            return False
            
    except Exception as e:
        logger.error(f"Error checking authorization: {str(e)}")
        return False

async def get_login_status(ctx: BrowserContext) -> dict:
    """Get detailed LinkedIn login status including profile information if available."""
    page = await ctx.get_current_page()
    status = {
        "is_logged_in": False,
        "profile_name": None,
        "profile_url": None,
        "error": None
    }
    
    try:
        # First check if we're logged in
        is_authorized = await check_authorization(ctx)
        status["is_logged_in"] = is_authorized
        
        if is_authorized:
            try:
                # Try to get profile information with a longer timeout
                await page.wait_for_selector('.global-nav', timeout=5000)
                
                # Look for any of these selectors that might contain the profile name
                selectors = [
                    '.global-nav__me-photo',
                    '.profile-rail-card__actor-link',
                    '.feed-identity-module__actor-meta'
                ]
                
                for selector in selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            if selector == '.global-nav__me-photo':
                                alt_text = await element.get_attribute('alt')
                                if alt_text:
                                    status["profile_name"] = alt_text.replace("'s profile photo", "")
                                    break
                            else:
                                text = await element.text_content()
                                if text:
                                    status["profile_name"] = text.strip()
                                    break
                    except Exception:
                        continue
                
                # Try to get profile URL
                try:
                    profile_link = await page.query_selector('a[data-control-name="identity_profile_photo"]')
                    if profile_link:
                        href = await profile_link.get_attribute('href')
                        if href:
                            status["profile_url"] = href if href.startswith('http') else f"https://www.linkedin.com{href}"
                except Exception:
                    pass
                    
            except Exception as e:
                status["error"] = f"Error getting profile details: {str(e)}"
        else:
            status["error"] = "Not logged in to LinkedIn"
                
    except Exception as e:
        status["error"] = f"Error checking login status: {str(e)}"
    
    return status

async def ensure_url(ctx: BrowserContext, url: str) -> None:
    page = await ctx.get_current_page()
    current_url = page.url

    if not fnmatch(current_url, url + '*'):
        logger.info(f'Navigating to {url} from {current_url}')
        await page.goto(url, wait_until='domcontentloaded')

    return fnmatch(current_url, url + '*')

async def sign_out(browser: BrowserContext):
    sites = ['www.linkedin.com', 'linkedin.com']

    for site in sites:
        await browser.session.context.clear_cookies(domain=site)
        
    page = await browser.get_current_page()
    await page.reload(wait_until='load')

    return ActionResult(extracted_content='Sign out successful!')

# @_controller.action('Open the user mail box')
async def open_mail_box(browser: BrowserContext):
    page = await browser.get_current_page()
    
    await page.goto('https://mail.google.com/mail/u/0/', wait_until='domcontentloaded')
    await page.wait_for_selector('div[role="main"]', timeout=5000)

    return ActionResult(extracted_content='Navigated to input box')

async def fill_email_form(browser: BrowserContext, subject: str, body: str, recipient: str = None):
    page = await browser.get_current_page()

    if recipient is not None:
        await page.fill('input[aria-label="To recipients"]', recipient)

    await page.fill('input[name="subjectbox"]', subject)
    await page.fill('div[aria-label="Message Body"]', body)

    return ActionResult(extracted_content='Email form filled successfully!')

async def search_email(browser: BrowserContext, query: str):
    page = await browser.get_current_page()

    # Wait for the search box to be available
    await page.wait_for_selector('input[name="q"]', timeout=5000)
    await page.fill('input[name="q"]', query)
    await page.click('button[aria-label="Search mail"]')

    return ActionResult(extracted_content='Search executed successfully!')

def get_basic_controler():
    global _controller
