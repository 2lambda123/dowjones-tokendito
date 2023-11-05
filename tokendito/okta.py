# vim: set filetype=python ts=4 sw=4
# -*- coding: utf-8 -*-
"""
Handle the all Okta operations.

1. Okta authentication
2. Update Okta Config File

"""
import base64
import codecs
from copy import deepcopy
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib
import uuid

import bs4
from bs4 import BeautifulSoup

# import requests
from tokendito import duo
from tokendito import user
from tokendito.http_client import HTTP_client
from tokendito import __version__

from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_status_dict = dict(
    E0000004="Authentication failed",
    E0000047="API call exceeded rate limit due to too many requests",
    PASSWORD_EXPIRED="Your password has expired",
    LOCKED_OUT="Your account is locked out",
)


def api_error_code_parser(status=None):
    """Status code parsing.

    param status: Response status
    return message: status message
    """
    logger.debug(f"api_error_code_parser({status})")
    if status and status in _status_dict.keys():
        message = f"Okta auth failed: {_status_dict[status]}"
    else:
        message = f"Okta auth failed: {status}. Please verify your settings and try again."
    logger.debug(f"Parsing error [{message}] ")
    return message


def get_auth_pipeline(url=None):
    """Get auth pipeline version."""
    logger.debug(f"get_auth_pipeline({url})")
    headers = {"accept": "application/json"}
    # https://developer.okta.com/docs/api/openapi/okta-management/management/tag/OrgSetting/
    url = f"{url}/.well-known/okta-organization"

    response = HTTP_client.get(url, headers=headers)

    try:
        response_json = response.json()
    except (KeyError, ValueError) as e:
        logger.error(f"Failed to parse type in {url}:{str(e)}")
        logger.debug(f"Response: {response.text}")
        sys.exit(1)
    logger.debug(f"we have {response_json}")
    try:
        auth_pipeline = response_json.get("pipeline", None)
    except (KeyError, ValueError) as e:
        logger.error(f"Failed to parse pipeline in {url}:{str(e)}")
        logger.debug(f"Response: {response.text}")
        sys.exit(1)
    if auth_pipeline != "idx" and auth_pipeline != "v1":
        logger.error(f"unsupported auth pipeline version {auth_pipeline}")
        sys.exit(1)
    logger.debug(f"Pipeline is of type {auth_pipeline}")
    return auth_pipeline


def get_auth_properties(userid=None, url=None):
    """Make a call to the webfinger endpoint to get the auth properties metadata.
    :param userid: User's ID for which we are requesting an auth endpoint.
    :param url: Okta organization URL where we are looking up the user.
    :returns: Dictionary containing authentication properties.
    """
    logger.debug(f"get_auth_properies({userid}, {url})")
    payload = {"resource": f"okta:acct:{userid}", "rel": "okta:idp"}
    # payload = {"resource": f"okta:acct:{userid}"}
    headers = {"accept": "application/jrd+json"}
    url = f"{url}/.well-known/webfinger"
    logger.debug(f"Looking up auth endpoint for {userid} in {url}")

    # Make a GET request to the webfinger endpoint.
    response = HTTP_client.get(url, params=payload, headers=headers)

    # Extract properties from the response.
    try:
        ret = response.json()["links"][0]["properties"]
    except (KeyError, ValueError) as e:
        logger.error(f"Failed to parse authentication type in {url}:{str(e)}")
        logger.debug(f"Response: {response.text}")
        sys.exit(1)

    # Extract specific authentication properties if available.
    # Return a dictionary with 'metadata', 'type', and 'id' keys.
    properties = {}
    properties["metadata"] = ret.get("okta:idp:metadata", None)
    properties["type"] = ret.get("okta:idp:type", None)
    properties["id"] = ret.get("okta:idp:id", None)

    logger.debug(f"Auth properties are {properties}")
    return properties


def get_saml_request(auth_properties):
    """
    Get a SAML Request object from the Service Provider, to be submitted to the IdP.

    :param auth_properties: dict with the IdP ID and type.
    :returns: dict with post_url, relay_state, and base64 encoded saml request.
    """
    # Prepare the headers for the request to retrieve the SAML request.
    headers = {"accept": "text/html,application/xhtml+xml,application/xml"}

    # Build the URL based on the metadata and ID provided in the auth properties.
    base_url = user.get_base_url(auth_properties["metadata"])
    url = f"{base_url}/sso/idps/{auth_properties['id']}"

    logger.debug(f"Getting SAML request from {url}")

    # Make a GET request using the HTTP client to retrieve the SAML request.
    response = HTTP_client.get(url, headers=headers)

    # Extract the required parameters from the SAML request.
    saml_request = {
        "base_url": user.get_base_url(extract_form_post_url(response.text)),
        "post_url": extract_form_post_url(response.text),
        "relay_state": extract_saml_relaystate(response.text),
        "request": extract_saml_request(response.text, raw=True),
    }

    # Mask sensitive data in the logs for security.
    user.add_sensitive_value_to_be_masked(saml_request["request"])

    logger.debug(f"SAML request is {saml_request}")
    return saml_request


def send_saml_request(saml_request, cookies):
    """
    Submit SAML request to IdP, and get the response back.

    :param saml_request: dict with IdP post_url, relay_state, and saml_request
    :param cookies: session cookies with `sid`
    :returns: dict with with SP post_url, relay_state, and saml_response
    """
    logger.debug(
        f"""
                    send_saml_request 
                    HTTP_client cookies is {HTTP_client.session.cookies}")
                    
                    we'll set them to {cookies}
                    """
    )
    HTTP_client.set_cookies(cookies)
    # Define the payload and headers for the request
    payload = {
        "relayState": saml_request["relay_state"],
        "SAMLRequest": saml_request["request"],
    }

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml",
        "Content-Type": "application/json",
    }

    # Construct the URL from the provided saml_request
    url = saml_request["post_url"]

    # Log the SAML request details
    logger.debug(f"Sending SAML request to {url}")

    # Use the HTTP client to make a GET request
    response = HTTP_client.get(url, params=payload, headers=headers)

    # Extract relevant information from the response to form the saml_response dictionary
    saml_response = {
        "response": extract_saml_response(response.text, raw=True),
        "relay_state": extract_saml_relaystate(response.text),
        "post_url": extract_form_post_url(response.text),
    }

    # Mask sensitive values for logging purposes
    user.add_sensitive_value_to_be_masked(saml_response["response"])

    # Log the formed SAML response
    # logger.debug(f"SAML response is {saml_response}")
    logger.debug(
        f"""
                 After SAML Request call, 
                 we have HTTP_client.session cookies at {HTTP_client.session.cookies}
                 """
    )

    # Return the formed SAML response
    return saml_response


def set_oauth2_redirect_params_cookies(config, url):
    """
    Set OAuth redirect cookies for the HTTP client, needed for SAML2 flow for OIE

    okta-oauth-redirect-params={%22
    responseType%22:%22code%22%2C%22
    state%22:%22QkfKcTZ5uBV6dQePa72GdjB0h961QVNo0hgwvo6ya3oSqGQXAzRl1jzwOfnii9no%22%2C%22i
    nonce%22:%22Q2Bop0CFTqrPszYNc7uvgEpyAn9We2PTdYoKH09VuP2s0axlubkc7zWz7DgcqWtE%22%2C%22
    scopes%22:[%22openid%22%2C%22profile%22%2C%22email%22%2C%22okta.users.read.self%22%2C%22okta.users.manage.self%22%2C%22okta.internal.enduser.read%22%2C%22okta.intern      al.enduser.manage%22%2C%22okta.enduser.dashboard.read%22%2C%22okta.enduser.dashboard.manage%22]%2C%22
    clientId%22:%22okta.2b1959c8-bcc0-56eb-a589-cfcfb7422f26%22%2C%22
    urls%22:{%22issuer%22:%22https://newscorpdev2.oktapreview.com%22%2C%22
    authorizeUrl%22:%22https://newscorpdev2.oktapreview.com/oauth2/v1/authorize%22%2C%22
    userinfoUrl%22:%22https://newscorpdev2.oktapreview.com/oauth2/v1/userinfo%22%2C%22
    tokenUrl%22:%22https://newscorpdev2.oktapreview.com/oauth2/v1/token%22%2C%22
    revokeUrl%22:%22https://newscorpdev2.oktapreview.com/oauth2/v1/revoke%22%2C%22
    logoutUrl%22:%22https://newscorpdev2.oktapreview.com/oauth2/v1/logout%22}%2C%22
    ignoreSignature%22:false};
    okta-oauth-nonce=Q2Bop0CFTqrPszYNc7uvgEpyAn9We2PTdYoKH09VuP2s0axlubkc7zWz7DgcqWtE;
    okta-oauth-state=QkfKcTZ5uBV6dQePa72GdjB0h961QVNo0hgwvo6ya3oSqGQXAzRl1jzwOfnii9no; ln=svc_djif_tokendito_okta_aws_sandbox@okta.local;
    #okta_oauth_redirect_params={
    #    "responseType":"code",
    #    "state":"QkfKcTZ5uBV6dQePa72GdjB0h961QVNo0hgwvo6ya3oSqGQXAzRl1jzwOfnii9no",
    #    "scopes"
    #}
    # HTTP_client.session.cookies.set("okta-oauth-redirect-params", oie_data, path="/")
    """
    oauth2_config = get_oauth2_configuration(url)

    oauth_config_reformatted = {
        "responseType": oauth2_config["response_type"],
        "state": oauth2_config["state"],
        "clientID": oauth2_config(config),
        "tokenUrl": oauth2_config["token_endpoint"],
        "authorizeUrl": oauth2_config["authorization_endpoint"],
        "revokeUrl": oauth2_config["revocation_endpoint"],
        "logoutURL": oauth2_config["end_session_endpoint"],
        "scopes": oauth2_config["scope"],
        "okta-oauth-state": oauth2_config["state"],
    }
    cookies = {"okta-oauth-redirect-params": urllib.parse.urlencode(oauth_config_reformatted)}
    HTTP_client.set_cookies(cookies)


def send_saml_response(config, saml_response):
    """
    Submit SAML response to the SP.

    :param saml_response: dict with SP post_url, relay_state, and saml_response
    :returns: `sid` session cookie
    """
    # Define the payload and headers for the request.
    payload = {
        "SAMLResponse": saml_response["response"],
        "RelayState": saml_response["relay_state"],
    }
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    url = saml_response["post_url"]

    # Log the SAML response details.
    logger.debug(
        f""" send_saml_response

                    Sending SAML response back to {url}
                    and HTTP_client session cookies is {HTTP_client.session.cookies}
                    """
    )

    # Use the HTTP client to make a POST request.
    response = HTTP_client.post(url, data=payload, headers=headers)

    # Extract cookies from the response.
    session_cookies = response.cookies

    # Get the 'sid' value from the cookies.

    sid = session_cookies.get("sid")

    # If 'sid' is present, mask its value for logging purposes.
    #    if sid is not None:
    #        user.add_sensitive_value_to_be_masked(sid)

    # Log the session cookies.
    logger.debug(f"Have session cookies: {session_cookies}")
    # Extract the state token from the response.
    state_token = extract_state_token(response.text)
    if state_token:
        # set_oauth2_redirect_params_cookies(config, "https://newscorpdev2.oktapreview.com/")
        # TODO:
        # add auth cookies
        # with oie auth params

        myorg = "https://newscorpdev2.oktapreview.com"
        myurl = f"{myorg}/login/token/redirect"
        # myurl = f"{myorg}/api/v1/authn"
        myresponse = HTTP_client.get(
            # myurl, allow_redirects=False, params={"stateToken": state_token}
            myurl,
            params={"stateToken": state_token},
        )
        logger.debug(
            f"State token from {url}: {state_token} - FIXME bring this back the calling stack"
        )
        session_cookies = myresponse.cookies
    logger.debug(f"in send SAML response, we return {session_cookies}")

    # Return the session cookies.
    return session_cookies


def get_session_token(config, primary_auth, headers):
    """Get session_token.

    :param config: Configuration object
    :param headers: Headers of the request
    :param primary_auth: Primary authentication
    :return: Session Token from JSON response
    """
    try:
        status = primary_auth.get("status", None)
    except AttributeError:
        pass

    if status == "SUCCESS" and "sessionToken" in primary_auth:
        session_token = primary_auth.get("sessionToken")
    elif status == "MFA_REQUIRED":
        # Note: mfa_challenge should also be modified to accept and use http_client
        session_token = mfa_challenge(config, headers, primary_auth)
    else:
        logger.debug(f"Error parsing response: {json.dumps(primary_auth)}")
        logger.error(f"Okta auth failed: unknown status {status}")
        sys.exit(1)

    user.add_sensitive_value_to_be_masked(session_token)

    return session_token


def get_oauth2_token(config, authz_code_flow_data, authorize_code):
    """Get OAuth token from Okta by calling /token endpoint.

    https://developer.okta.com/docs/reference/api/oidc/#token-endpoint
    :param url: URL of the Okta OAuth token endpoint
    :return: OAuth token
    """
    payload = {
        "code": authorize_code,
        "state": authz_code_flow_data["state"],
        "grant_type": authz_code_flow_data["grant_type"],
        "redirect_uri": authz_code_flow_data["redirect_uri"],
        "client_id": authz_code_flow_data["client_id"],
        "code_verifier": authz_code_flow_data["code_verifier"],
    }
    # payload = {"resource": f"okta:acct:{userid}"}
    # headers = {"accept": "application/jrd+json"}
    # response = user.request_wrapper("GET", token_endpoint_url , headers=headers, params=payload)

    headers = {"accept": "application/json"}
    # Using the http_client to make the POST request
    response_json = HTTP_client.post(
        authz_code_flow_data["token_endpoint_url"], data=payload, headers=headers, return_json=True
    )
    return response_json


def extract_authz_code(response_text):
    """Extract authorization code from response text.

    :param response_text: response text from /authorize call
    :return: authorization code
    """
    authz_code = re.search(r"(?<=code=)[^&]+", response_text).group(0)
    return authz_code


def get_client_id(config):
    """Returns the client id needed by the Authorization Code Flow.

    If a command line parameter was passed, it will take precedence.
    Until we figure out how to get is value, is has to be a parameter.
    see https://developer.okta.com/docs/reference/api/oauth-clients/
    """
    if config.okta["oauth_client_id"] is None:
        config.okta[
            "oauth_client_id"
        ] = f"okta.{str(uuid.uuid4())}"  # note: this client_id does not work.
    return config.okta["oauth_client_id"]


def get_redirect_uri(config):
    "Returns the redirect uri needed by the Authorization Code Flow."

    url = f"{config.okta['org']}/enduser/callback"
    return url


def get_response_type():
    "We're only implementing code response type."
    return "code"


def get_authorize_scope():
    "We're only implementing openid scope."
    # return "openid" # most likely the one to use, to confirm
    # return "openid profile"
    return "openid profile email okta.users.read.self okta.users.manage.self okta.internal.enduser.read okta.internal.enduser.manage okta.enduser.dashboard.read okta.enduser.dashboard.manage"


def get_oauth2_state():
    """Generate a random string for state.
    https://developer.okta.com/docs/guides/implement-grant-type/authcode/main/#flow-specifics
    https://developer.okta.com/docs/guides/implement-grant-type/authcodepkce/main/#next-steps
    """
    state = hashlib.sha256(os.urandom(1024)).hexdigest()
    return state


def get_pkce_code_challenge_method():
    """TODO"""
    return "S256"


def get_pkce_code_challenge(code_verifier=None):
    """
    get_pkce_code_challenge

    Base64-URL-encoded string of the SHA256 hash of the code verifier
    https://www.oauth.com/oauth2-servers/pkce/authorization-request/

    :param: code_verifier
    :return: code_challenge
    """

    code_challenge = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(code_challenge).decode("utf-8")
    code_challenge = code_challenge.replace("=", "")
    return code_challenge


def get_pkce_code_verifier():
    """
    to review
    """
    # code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8")
    # code_verifier = base64.urlsafe_b64encode(os.urandom(50)).rstrip(b'=')

    code_verifier = base64.urlsafe_b64encode(os.urandom(40)).decode("utf-8")
    code_verifier = re.sub("[^a-zA-Z0-9]+", "", code_verifier)
    return code_verifier


def pkce_enabled():
    """TODO"""
    return True


def get_idx_cookies(cookies):
    """get idx cookies from response"""
    idx_val = cookies.get("idx")
    return idx_val


def get_authorize_code(response, payload):
    """
    Get the authorize code

    This will exit with error if we cannot get the code.
    It will also check the response from the /authorize call for callback errors,
    And if any, print and exit with error.
    """
    callback_url = response.url
    error_code = re.search(r"(?<=error=)[^&]+", callback_url)
    error_desc = re.search(r"(?<=error_description=)[^&]+", callback_url)
    if error_code:
        logger.error(
            f"""
            oath2 callback error:{error_code.group()} - description:{error_desc.group()}
            payload sent: {payload}
            """
        )
        sys.exit(1)
    authorize_code = re.search(r"(?<=code=)[^&]+", callback_url)
    authorize_state = re.search(r"(?<=state=)[^&]+", callback_url).group()
    if authorize_code:
        return authorize_code.group()


def oauth2_authorize_request(config, authz_code_flow_data, session_cookies):
    """implements authorization code request
    calls /authorize endpoint with authenticated session_token.
    https://developer.okta.com/docs/reference/api/oidc/#_2-okta-as-the-identity-platform-for-your-app-or-api
    :param
    :return: authorization code, needed for /token call
    """
    logger.debug(f"oauth_code_request({config}, {authz_code_flow_data}, {session_cookies})")
    headers = {"accept": "application/json", "content-type": "application/json"}
    # headers = {"accept": "application/json", "content-type": "application/x-www-form-urlencoded"}

    session_token = None
    if session_cookies is not None:
        session_token = session_cookies.get("session_token")

    payload = {
        "client_id": authz_code_flow_data["client_id"],
        "redirect_uri": authz_code_flow_data["redirect_uri"],
        "response_type": authz_code_flow_data["response_type"],
        "sessionToken": session_token,
        "scope": authz_code_flow_data["scope"],
        "state": authz_code_flow_data["state"],
        "code_challenge": authz_code_flow_data["code_challenge"],
        "code_challenge_method": authz_code_flow_data["code_challenge_method"],
        "prompt": "none",  # dont authenticate
    }
    response = HTTP_client.get(
        authz_code_flow_data["authz_endpoint_url"],
        headers=headers,
        params=payload,
    )

    authorize_code = get_authorize_code(response, payload)

    logger.debug(f"Cookies in session: {HTTP_client.session.cookies.get_dict()}")

    # session_cookies = get_idx_cookies(HTTP_client.session.cookies)

    return authorize_code


def authorization_code_flow(config, oauth2_config, session_cookies):
    # Authorization Code flow (see
    # https://developer.okta.com/docs/guides/implement-grant-type/authcode/main/#about-the-authorization-code-grant
    # )

    authz_code_flow_data = {
        "client_id": get_client_id(config),
        "redirect_uri": get_redirect_uri(config),
        "response_type": get_response_type(),
        "scope": get_authorize_scope(),
        "state": get_oauth2_state(),
        "authz_endpoint_url": oauth2_config["authorization_endpoint"],
        "token_endpoint_url": oauth2_config["token_endpoint"],
        "grant_type": "authorization_code",
    }

    if pkce_enabled():
        code_verifier = get_pkce_code_verifier()
        authz_code_flow_data["code_verifier"] = code_verifier
        authz_code_flow_data["code_challenge"] = get_pkce_code_challenge(code_verifier)
        authz_code_flow_data["code_challenge_method"] = get_pkce_code_challenge_method()

    # authz_code = oauth2_authorize_request(config, authz_code_flow_data, session_token)
    authorize_code = oauth2_authorize_request(config, authz_code_flow_data, session_cookies)

    authz_token = get_oauth2_token(config, authz_code_flow_data, authorize_code)
    user.add_sensitive_value_to_be_masked(authz_token)
    return authz_token


def authorization_code_enabled(org_url, oauth2_config):
    """Determines if authorization code grant is enabled
    returns True if enabled and False otherwise,
    """
    try:
        if "authorization_code" not in oauth2_config["grant_types_supported"]:
            return False
    except (KeyError, ValueError) as e:
        logger.error(f"No grant types supported on {org_url}:{str(e)}")
        sys.exit(1)

    return True


def get_oauth2_configuration(url=None):
    """Get authorization server configuration data from Okta instance.
    :param url: URL of the Okta org
    :return: dict of conguration values
    """
    url = f"{url}/.well-known/oauth-authorization-server"
    headers = {"accept": "application/json"}
    response = HTTP_client.get(url, headers=headers)
    logger.debug(f"Authorization Server info: {response.json()}")
    # todo: handle errors.
    oauth2_config = response.json()
    validate_oauth2_configuration(oauth2_config)
    return oauth2_config


def validate_oauth2_configuration(oauth2_config):
    """Validate that the oauth2 configuration has our implementation.
    :param oauth2_config: dict of configuration values
    """
    mandadory_oauth2_config_items = {
        "authorization_endpoint",
        "token_endpoint",
        "grant_types_supported",
        "response_types_supported",
        "scopes_supported",
    }  # the authorization server must have these config elements
    for item in mandadory_oauth2_config_items:
        if item not in oauth2_config:
            logger.error(f"No {item} found in oauth2 configuration.")
            sys.exit(1)

    if "authorization_code" not in oauth2_config["grant_types_supported"]:
        logger.error("Authorization code grant not found.")
        sys.exit(1)
    if "code" not in oauth2_config["response_types_supported"]:
        logger.error("Code response type not found.")
        sys.exit(1)


def oauth2_authorize(config, session_cookies):
    """Authorize on the Okta authorization server, following oauth2 flows
    returns a token
    """
    logger.debug(f"oie_authorize({config}, {session_cookies})")

    oauth2_config = get_oauth2_configuration(config.okta["org"])
    if authorization_code_enabled(config.okta["org"], oauth2_config):
        authz_token = authorization_code_flow(config, oauth2_config, session_cookies)
    else:
        logger.warning(
            f"Authorization Code is not enabled on {config.okta['org']}, skipping oauth2"
        )
        authz_cookies = session_cookies
    return session_cookies  # for now, pass thru


def create_sid_cookies(authn_org_url, session_token):
    """
    Create session cookie.

    :param authn_org_url: org url
    :param session_token: session token, str
    :returns: cookies jar with session_id value we got using the token
    """
    # Construct the URL from the base URL provided.
    url = f"{authn_org_url}/api/v1/sessions"

    # Define the payload and headers for the request.
    data = {"sessionToken": session_token}
    headers = {"Content-Type": "application/json", "accept": "application/json"}

    # Log the request details.
    logger.debug(f"Requesting session cookies from {url}")

    # Use the HTTP client to make a POST request.
    response_json = HTTP_client.post(url, json=data, headers=headers, return_json=True)
    if "id" not in response_json:
        logger.error(f"'id' not found in response. Full response: {response_json}")
        sys.exit(1)

    session_id = response_json["id"]
    # cookies = requests.cookies.RequestsCookieJar()
    # cookies.set("sid", session_id, path="/")  # set global cookies with our session id
    cookies = None
    return cookies


def idp_auth(config):
    """authenticate and authorize with the IDP. Authorization happens if OIE
    is enabled, with Authorization code flow and PKCE being the only implemented grant types.

    :param config: Config object
    :return: session ID cookie.
    """
    logger.debug(f"idp_auth({config})")
    auth_properties = get_auth_properties(userid=config.okta["username"], url=config.okta["org"])

    if "type" not in auth_properties:
        logger.error("Okta auth failed: unknown type.")
        sys.exit(1)

    session_cookies = None  # both authn and authz session_cookies
    session_token = None  # authn token we get when authentating
    authn_cookies = None  # the authentication cookies

    logger.debug(
        f"""
                    ======== >>>>> 
                    GOING TO {config.okta['org']}
                    ======= 
                    """
    )

    if local_authentication_enabled(auth_properties):
        logger.debug(
            """
                        idp authenticate

                        """
        )
        session_token = user_authenticate(config)
        # session_cookies = create_sid_cookies(config.okta["org"], session_token)
        session_cookies = user.request_cookies(config.okta["org"], session_token)
        HTTP_client.set_cookies(session_cookies)  # save cookies for later use in authorize call.
        logger.debug(
            f"""
            idp_authenticate returns session cookies:{session_cookies}
            """
        )
    elif is_saml2_authentication(auth_properties):
        logger.debug(
            f"""
            saml2_authenticate
            calling saml2_authenticate, just before we have client cookies: {HTTP_client.session.cookies}
            """
        )
        session_cookies = saml2_authenticate(config, auth_properties)
        logger.debug(
            f"""
            Just after saml2_authenticate we have client cookies: {HTTP_client.session.cookies}

            and saml2_authenticate returns session_cookies = {session_cookies}

            """
        )
    else:
        logger.error(f"{auth_properties['type']} login via IdP Discovery is not curretly supported")
        sys.exit(1)

    # Once we get there, the user is authenticated. The session_cookies is either from local authentication
    # or the one returned by saml2 if o2o.
    if oie_enabled(config.okta["org"]):
        logger.debug(
            f"""
                    oauth2_authorize
                    _session_cookies: {HTTP_client.session.cookies}

                    """
        )
        # session_cookies =
        session_cookies = oauth2_authorize(config, session_cookies)

    logger.debug(f"Returning session cookies: {session_cookies}")
    return session_cookies


def oie_enabled(url):
    """
    Determines if OIE is enabled.
    :pamam url: okta org url
    :return: True if OIE is enabled, False otherwise
    """
    if get_auth_pipeline(url) == "idx":  # oie
        return True
    else:
        return False


def user_authenticate(config):
    """Authenticate user on local okta instance.

    :param config: Config object
    :return: auth session ID cookie.
    """

    logger.debug(f"user_authenticate({config}")
    session_token = None
    headers = {"content-type": "application/json", "accept": "application/json"}
    payload = {"username": config.okta["username"], "password": config.okta["password"]}

    logger.debug(f"Authenticate user to {config.okta['org']}")
    logger.debug(f"Sending {headers}, {payload} to {config.okta['org']}")

    primary_auth = HTTP_client.post(
        f"{config.okta['org']}/api/v1/authn", json=payload, headers=headers, return_json=True
    )

    if "errorCode" in primary_auth:
        api_error_code_parser(primary_auth["errorCode"])
        sys.exit(1)

    while session_token is None:
        session_token = get_session_token(config, primary_auth, headers)
    logger.info(f"User has been successfully authenticated to {config.okta['org']}.")
    return session_token


def local_authentication_enabled(auth_properties):
    """Check whether authentication happens on the current instance.

    :param auth_properties: auth_properties dict
    :return: True if this is the place to authenticate, False otherwise.
    """
    try:
        if auth_properties["type"] == "OKTA":
            return True
    except (TypeError, KeyError):
        pass
    return False


def is_saml2_authentication(auth_properties):
    """Check whether authentication happens via SAML2 on a different IdP.

    :param auth_properties: auth_properties dict
    :return: True for SAML2 on Okta, False otherwise.
    """
    try:
        if auth_properties["type"] == "SAML2":
            return True
    except (TypeError, KeyError):
        pass
    return False


def saml2_authenticate(config, auth_properties):
    """SAML2 authentication flow.

    :param config: Config object
    :param auth_properties: dict with authentication properties
    :returns: session ID cookie, if successful.
    """
    # Get the SAML request details
    saml_request = get_saml_request(auth_properties)

    # Create a copy of our configuration, so that we can freely reuse it
    # without Python's pass-as-reference-value interfering with it.
    saml2_config = deepcopy(config)
    saml2_config.okta["org"] = saml_request["base_url"]
    logger.info(f"Authentication is being redirected to {saml2_config.okta['org']}.")

    # Try to authenticate using the new configuration. This could cause
    # recursive calls, which allows for IdP chaining.
    session_cookies = idp_auth(saml2_config)

    # Once we are authenticated, send the SAML request to the IdP.
    # This call requires session cookies.
    saml_response = send_saml_request(saml_request, session_cookies)

    # Send SAML response from the IdP back to the SP, which will generate new
    # session cookies.
    session_id = send_saml_response(config, saml_response)
    return session_id


def extract_saml_response(html, raw=False):
    """Parse html, and extract a SAML document.

    :param html: String with HTML document.
    :param raw: Boolean that determines whether or not the response should be decoded.
    :return: XML Document, or None
    """
    soup = BeautifulSoup(html, "html.parser")
    xml = None
    saml_base64 = None
    retval = None

    elem = soup.find("input", attrs={"name": "SAMLResponse"})
    if type(elem) is bs4.element.Tag:
        saml_base64 = str(elem.get("value"))
        xml = codecs.decode(saml_base64.encode("ascii"), "base64").decode("utf-8")

        retval = xml
        if raw:
            retval = saml_base64
    return retval


def extract_saml_request(html, raw=False):
    """Parse html, and extract a SAML document.

    :param html: String with HTML document.
    :param raw: Boolean that determines whether or not the response should be decoded.
    :return: XML Document, or None
    """
    soup = BeautifulSoup(html, "html.parser")
    xml = None
    saml_base64 = None
    retval = None

    elem = soup.find("input", attrs={"name": "SAMLRequest"})
    if type(elem) is bs4.element.Tag:
        saml_base64 = str(elem.get("value"))
        xml = codecs.decode(saml_base64.encode("ascii"), "base64").decode("utf-8")

        retval = xml
        if raw:
            retval = saml_base64
    return retval


def extract_form_post_url(html):
    """Parse html, and extract a Form Action POST URL.

    :param html: String with HTML document.
    :return: URL string, or None
    """
    soup = BeautifulSoup(html, "html.parser")
    post_url = None

    elem = soup.find("form", attrs={"id": "appForm"})
    if type(elem) is bs4.element.Tag:
        post_url = str(elem.get("action"))
    return post_url


def extract_saml_relaystate(html):
    """Parse html, and extract SAML relay state from a form.

    :param html: String with HTML document.
    :return: relay state value, or None
    """
    soup = BeautifulSoup(html, "html.parser")
    relay_state = None

    elem = soup.find("input", attrs={"name": "RelayState"})
    if type(elem) is bs4.element.Tag:
        relay_state = str(elem.get("value"))
    return relay_state


def extract_state_token(html):
    """Parse an HTML document, and extract a state token.

    :param html: String with HTML document
    :return: string with state token, or None
    """
    soup = BeautifulSoup(html, "html.parser")
    state_token = None
    pattern = re.compile(r"var stateToken = '(?P<stateToken>.*)';", re.MULTILINE)

    script = soup.find("script", text=pattern)
    if type(script) is bs4.element.Tag:
        match = pattern.search(script.text)
        if match:
            encoded_token = match.group("stateToken")
            state_token = codecs.decode(encoded_token, "unicode-escape")

    return state_token


def mfa_provider_type(
    config,
    mfa_provider,
    selected_factor,
    mfa_challenge_url,
    primary_auth,
    selected_mfa_option,
    headers,
    payload,
):
    """Receive session key.

    :param config: Config object
    :param mfa_provider: MFA provider
    :param selected_factor: Selected MFA factor
    :param mfa_challenge_url: MFA challenge url
    :param primary_auth: Primary authentication
    :param selected_mfa_option: Selected MFA option
    :return: session_key

    """
    mfa_verify = dict()
    factor_type = selected_factor.get("_embedded", {}).get("factor", {}).get("factorType", None)

    if mfa_provider == "DUO":
        payload, headers, callback_url = duo.authenticate_duo(selected_factor)
        duo.duo_api_post(callback_url, payload=payload)
        mfa_verify = HTTP_client.post(
            mfa_challenge_url, json=payload, headers=headers, return_json=True
        )

    elif mfa_provider == "OKTA" and factor_type == "push":
        mfa_verify = push_approval(mfa_challenge_url, payload)
    elif mfa_provider in ["OKTA", "GOOGLE"] and factor_type in ["token:software:totp", "sms"]:
        mfa_verify = totp_approval(
            config, selected_mfa_option, headers, mfa_challenge_url, payload, primary_auth
        )
    else:
        logger.error(
            f"Sorry, the MFA provider '{mfa_provider}:{factor_type}' is not yet supported."
            " Please retry with another option."
        )
        sys.exit(1)

    if "sessionToken" not in mfa_verify:
        logger.error(
            f"Could not verify MFA Challenge with {mfa_provider} {primary_auth['factorType']}"
        )
    return mfa_verify["sessionToken"]


def mfa_index(preset_mfa, available_mfas, mfa_options):
    """Get mfa index in request.

    :param preset_mfa: preset mfa from settings
    :param available_mfas: available mfa ids
    :param mfa_options: available mfas
    """
    indices = []
    # Gets the index number from each preset MFA in the list of avaliable ones.
    if preset_mfa:
        logger.debug(f"Get mfa from {available_mfas}.")
        indices = [i for i, elem in enumerate(available_mfas) if preset_mfa in elem]

    index = None
    if len(indices) == 0:
        logger.debug(f"No matches with {preset_mfa}, going to get user input")
        index = user.select_preferred_mfa_index(mfa_options)
    elif len(indices) == 1:
        logger.debug(f"One match: {preset_mfa} in {indices}")
        index = indices[0]
    else:
        logger.error(
            f"{preset_mfa} is not unique in {available_mfas}. Please check your configuration."
        )
        sys.exit(1)

    return index


def mfa_challenge(config, headers, primary_auth):
    """Handle user mfa challenges.

    :param config: Config object
    :param headers: headers what needs to be sent to api
    :param primary_auth: primary authentication
    :return: Okta MFA Session token after the successful entry of the code
    """
    logger.debug("Handle user MFA challenges")
    try:
        mfa_options = primary_auth["_embedded"]["factors"]
    except KeyError as error:
        logger.error(f"There was a wrong response structure: \n{error}")
        sys.exit(1)

    preset_mfa = config.okta["mfa"]

    available_mfas = [f"{d['provider']}_{d['factorType']}_{d['id']}" for d in mfa_options]
    index = mfa_index(preset_mfa, available_mfas, mfa_options)

    selected_mfa_option = mfa_options[index]
    logger.debug(f"Selected MFA is [{selected_mfa_option}]")

    mfa_challenge_url = selected_mfa_option["_links"]["verify"]["href"]

    payload = {
        "stateToken": primary_auth["stateToken"],
        "factorType": selected_mfa_option["factorType"],
        "provider": selected_mfa_option["provider"],
        "profile": selected_mfa_option["profile"],
    }

    selected_factor = HTTP_client.post(
        mfa_challenge_url, json=payload, headers=headers, return_json=True
    )

    mfa_provider = selected_factor["_embedded"]["factor"]["provider"]
    logger.debug(f"MFA Challenge URL: [{mfa_challenge_url}] headers: {headers}")

    mfa_session_token = mfa_provider_type(
        config,
        mfa_provider,
        selected_factor,
        mfa_challenge_url,
        primary_auth,
        selected_mfa_option,
        headers,
        payload,
    )

    logger.debug(f"MFA Session Token: [{mfa_session_token}]")
    return mfa_session_token


def totp_approval(config, selected_mfa_option, headers, mfa_challenge_url, payload, primary_auth):
    """Handle user mfa options.

    :param config: Config object
    :param selected_mfa_option: Selected MFA option (SMS, push, etc)
    :param headers: headers
    :param mfa_challenge_url: MFA challenge URL
    :param payload: payload
    :param primary_auth: Primary authentication method
    :return: payload data

    """
    logger.debug(f"User MFA options selected: [{selected_mfa_option['factorType']}]")
    if config.okta["mfa_response"] is None:
        logger.debug("Getting verification code from user.")
        config.okta["mfa_response"] = user.get_input("Enter your verification code: ")
        user.add_sensitive_value_to_be_masked(config.okta["mfa_response"])

    # time to verify the mfa
    payload = {
        "stateToken": primary_auth["stateToken"],
        "passCode": config.okta["mfa_response"],
    }

    # Using the http_client to make the POST request
    mfa_verify = HTTP_client.post(
        mfa_challenge_url, json=payload, headers=headers, return_json=True
    )

    if "sessionToken" in mfa_verify:
        user.add_sensitive_value_to_be_masked(mfa_verify["sessionToken"])
    logger.debug(f"mfa_verify [{json.dumps(mfa_verify)}]")

    return mfa_verify


def push_approval(mfa_challenge_url, payload):
    """Handle push approval from the user.

    :param mfa_challenge_url: MFA challenge url
    :param payload: payload which needs to be sent
    :return: Session Token if succeeded or terminates if user wait goes 5 min

    """
    logger.debug(f"Push approval with challenge_url:{mfa_challenge_url}")

    user.print("Waiting for an approval from the device...")
    status = "MFA_CHALLENGE"
    result = "WAITING"
    response = {}
    challenge_displayed = False

    headers = {"content-type": "application/json", "accept": "application/json"}

    while status == "MFA_CHALLENGE" and result == "WAITING":
        response = HTTP_client.post(
            mfa_challenge_url, json=payload, headers=headers, return_json=True
        )

        if "sessionToken" in response:
            user.add_sensitive_value_to_be_masked(response["sessionToken"])

        logger.debug(f"MFA Response:\n{json.dumps(response)}")
        # Retrieve these values from the object, and set a sensible default if they do not
        # exist.
        status = response.get("status", "UNKNOWN")
        result = response.get("factorResult", "UNKNOWN")

        # The docs at https://developer.okta.com/docs/reference/api/authn/#verify-push-factor
        # state that the call will return a factorResult in [ SUCCESS, REJECTED, TIMEOUT,
        # WAITING]. However, on success, SUCCESS is not set and we have to rely on the
        # response["status"] instead
        answer = (
            response.get("_embedded", {})
            .get("factor", {})
            .get("_embedded", {})
            .get("challenge", {})
            .get("correctAnswer", None)
        )
        if answer and not challenge_displayed:
            # If a Number Challenge response exists, retrieve it from this deeply nested path,
            # otherwise set to None.
            user.print(f"Number Challenge response is {answer}")
            challenge_displayed = True
        time.sleep(1)

    if status == "SUCCESS" and "sessionToken" in response:
        # noop, we will return the variable later
        pass
    # Everything else should have a status of "MFA_CHALLENGE", and the result provides a
    # hint on why the challenge failed.
    elif result == "REJECTED":
        logger.error("The Okta Verify push has been denied.")
        sys.exit(2)
    elif result == "TIMEOUT":
        logger.error("Device approval window has expired.")
        sys.exit(2)
    else:
        logger.error(f"Push response type {result} for {status} not implemented.")
        sys.exit(2)

    return response
