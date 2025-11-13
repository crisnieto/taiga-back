# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2021-present Kaleidos INC

import logging
from typing import List, Optional, Dict, Any

from django.conf import settings

from taiga.base.mails import mail_builder

logger = logging.getLogger(__name__)


def send_feedback(
    feedback_entry: Any,
    extra: Dict[str, Any],
    reply_to: Optional[List[str]] = None
) -> bool:
    """
    Send feedback notification email to support.

    :param feedback_entry: The feedback entry object to include in the email.
    :param extra: Additional context data to include in the email.
    :param reply_to: Optional list of email addresses for the Reply-To header.
                    Defaults to empty list if not provided.

    :return: True if email was sent successfully, False otherwise.
    """
    if reply_to is None:
        reply_to = []

    support_email = settings.FEEDBACK_EMAIL

    if not support_email:
        logger.warning("FEEDBACK_EMAIL not configured, skipping feedback notification")
        return False

    try:
        reply_to.append(support_email)

        ctx = {
            "feedback_entry": feedback_entry,
            "extra": extra
        }

        email = mail_builder.feedback_notification(support_email, ctx)
        email.extra_headers["Reply-To"] = ", ".join(reply_to)
        email.send()
        logger.info("Feedback notification sent successfully to %s", support_email)
        return True
    except Exception as e:
        logger.exception("Error sending feedback notification: %s", str(e))
        return False
