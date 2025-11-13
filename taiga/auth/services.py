# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2021-present Kaleidos INC
#

from typing import Callable, Dict, Any, Tuple, Optional
import logging
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.db import IntegrityError
from django.db import transaction as tx
from django.utils.translation import gettext_lazy as _

from taiga.base import exceptions as exc
from taiga.base.mails import mail_builder
from taiga.users.models import User
from taiga.users.serializers import UserAdminSerializer
from taiga.users.services import get_and_validate_user
from taiga.projects.services.invitations import get_membership_by_token

from .exceptions import AuthenticationFailed, InvalidToken, TokenError
from .settings import api_settings
from .tokens import RefreshToken, CancelToken, UntypedToken
from .signals import user_registered as user_registered_signal

logger = logging.getLogger(__name__)


#####################
## AUTH PLUGINS
#####################

auth_plugins = {}


def register_auth_plugin(name: str, login_func: Callable):
    auth_plugins[name] = {
        "login_func": login_func,
    }


def get_auth_plugins():
    return auth_plugins


#####################
## AUTH SERVICES
#####################

def make_auth_response_data(user: User) -> Dict[str, Any]:
    """
    Generate authentication response data for a user.

    :param user: The authenticated user instance.
    :return: Dictionary containing user data, refresh token, and auth token.
    """
    serializer = UserAdminSerializer(user)
    data = dict(serializer.data)

    refresh = RefreshToken.for_user(user)

    data['refresh'] = str(refresh)
    data['auth_token'] = str(refresh.access_token)

    if api_settings.UPDATE_LAST_LOGIN:
        update_last_login(None, user)

    return data


def login(username: str, password: str) -> Dict[str, Any]:
    """
    Authenticate a user with username/email and password.

    :param username: Username or email address of the user.
    :param password: User's password.
    :return: Dictionary containing user data, refresh token, and auth token.
    :raises AuthenticationFailed: If credentials are invalid or user is inactive.
    """
    try:
        user = get_and_validate_user(username=username, password=password)
        logger.info("User %s logged in successfully", user.username)
    except exc.WrongArguments as e:
        logger.warning("Login failed for username: %s", username)
        raise AuthenticationFailed(
            _('No active account found with the given credentials'),
            'invalid_credentials',
        ) from e

    return make_auth_response_data(user)


def refresh_token(refresh_token: str) -> Dict[str, str]:
    """
    Refresh an authentication token using a refresh token.

    :param refresh_token: The refresh token string.
    :return: Dictionary containing new auth_token and optionally new refresh token.
    :raises InvalidToken: If the refresh token is invalid or expired.
    """
    try:
        refresh = RefreshToken(refresh_token)
    except TokenError as e:
        logger.warning("Invalid refresh token provided")
        raise InvalidToken() from e

    data = {'auth_token': str(refresh.access_token)}

    if api_settings.ROTATE_REFRESH_TOKENS:
        if api_settings.DENYLIST_AFTER_ROTATION:
            try:
                # Attempt to denylist the given refresh token
                refresh.denylist()
            except AttributeError:
                # If denylist app not installed, `denylist` method will
                # not be present
                logger.debug("Denylist app not installed, skipping token denylist")
                pass

        refresh.set_jti()
        refresh.set_exp()

        data['refresh'] = str(refresh)

    return data


def verify_token(token: str) -> Dict[str, Any]:
    """
    Verify if a token is valid.

    :param token: The token string to verify.
    :return: Empty dictionary if token is valid.
    :raises InvalidToken: If the token is invalid or expired.
    """
    try:
        UntypedToken(token)
        return {}
    except TokenError as e:
        logger.warning("Token verification failed")
        raise InvalidToken() from e


#####################
## REGISTER SERVICES
#####################

def send_register_email(user: User) -> bool:
    """
    Send registration welcome email to a newly registered user.

    :param user: The user instance to send the email to.
    :return: True if email was sent successfully, False otherwise.
    """
    try:
        cancel_token = CancelToken.for_user(user)
        context = {"user": user, "cancel_token": str(cancel_token)}
        email = mail_builder.registered_user(user, context)
        result = bool(email.send())
        if result:
            logger.info("Registration email sent successfully to %s", user.email)
        else:
            logger.warning("Failed to send registration email to %s", user.email)
        return result
    except Exception as e:
        logger.exception("Error sending registration email to %s: %s", user.email, str(e))
        return False


def is_user_already_registered(*, username: str, email: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a user with the given username or email is already registered.

    :param username: The username to check.
    :param email: The email address to check.
    :return: Tuple containing (is_registered: bool, error_message: str or None).
             If user exists, returns (True, error_message), otherwise (False, None).
    """
    user_model = get_user_model()
    if user_model.objects.filter(username__iexact=username).exists():
        logger.debug("Registration attempt with existing username: %s", username)
        return (True, _("Username is already in use."))

    if user_model.objects.filter(email__iexact=email).exists():
        logger.debug("Registration attempt with existing email: %s", email)
        return (True, _("Email is already in use."))

    return (False, None)


@tx.atomic
def public_register(
    username: str,
    password: str,
    email: str,
    full_name: str
) -> User:
    """
    Register a new user through the public registration flow.

    :param username: The desired username.
    :param password: The user's password.
    :param email: The user's email address.
    :param full_name: The user's full name.
    :return: The newly created User instance.
    :raises exc.WrongArguments: If username or email is already in use.
    :raises exc.IntegrityError: If there's a database integrity error.
    """
    is_registered, reason = is_user_already_registered(username=username, email=email)
    if is_registered:
        logger.warning("Public registration attempt with existing credentials: username=%s, email=%s", username, email)
        raise exc.WrongArguments(reason)

    user_model = get_user_model()
    user = user_model(
        username=username,
        email=email,
        email_token=str(uuid.uuid4()),
        new_email=email,
        verified_email=False,
        full_name=full_name,
        read_new_terms=True
    )
    user.set_password(password)
    try:
        user.save()
        logger.info("User registered successfully: %s (email: %s)", username, email)
    except IntegrityError as e:
        logger.error("Database integrity error during user registration: username=%s, email=%s", username, email)
        raise exc.WrongArguments(_("User is already registered.")) from e

    send_register_email(user)
    user_registered_signal.send(sender=user.__class__, user=user)
    return user


@tx.atomic
def private_register_for_new_user(
    token: str,
    username: str,
    email: str,
    full_name: str,
    password: str
) -> User:
    """
    Register a new user through the private invitation flow.

    This function registers a user using an invitation token and links
    them to the corresponding project membership.

    :param token: The invitation token.
    :param username: The desired username.
    :param email: The user's email address.
    :param full_name: The user's full name.
    :param password: The user's password.
    :return: The newly created User instance.
    :raises exc.WrongArguments: If username or email is already in use, or if token is invalid.
    :raises exc.IntegrityError: If there's a database integrity error.
    """
    is_registered, reason = is_user_already_registered(username=username, email=email)
    if is_registered:
        logger.warning("Private registration attempt with existing credentials: username=%s, email=%s", username, email)
        raise exc.WrongArguments(reason)

    user_model = get_user_model()
    user = user_model(
        username=username,
        email=email,
        full_name=full_name,
        email_token=str(uuid.uuid4()),
        new_email=email,
        verified_email=False,
        read_new_terms=True
    )

    user.set_password(password)
    try:
        user.save()
        logger.info("User registered via private invitation: %s (email: %s)", username, email)
    except IntegrityError as e:
        logger.error("Database integrity error during private user registration: username=%s, email=%s", username, email)
        raise exc.WrongArguments(_("Error while creating new user.")) from e

    try:
        membership = get_membership_by_token(token)
        membership.user = user
        membership.save(update_fields=["user"])
        logger.info("Membership linked to user %s via token", username)
    except Exception as e:
        logger.error("Error linking membership to user %s: %s", username, str(e))
        raise exc.WrongArguments(_("Invalid invitation token.")) from e

    send_register_email(user)
    user_registered_signal.send(sender=user.__class__, user=user)

    return user
