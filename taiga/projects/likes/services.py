# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2021-present Kaleidos INC

import logging
from typing import Optional, Any

from django.db.models import QuerySet
from django.db.transaction import atomic
from django.apps import apps
from django.contrib.auth import get_user_model

from .models import Like

logger = logging.getLogger(__name__)


def add_like(obj: Any, user) -> Like:
    """
    Add a like to an object.

    If the user has already liked the object nothing happens, so this function can be considered
    idempotent.

    :param obj: Any Django model instance.
    :param user: User adding the like. :class:`~taiga.users.models.User` instance.
    :return: Like instance (created or existing).
    """
    obj_type = apps.get_model("contenttypes", "ContentType").objects.get_for_model(obj)
    with atomic():
        like, created = Like.objects.get_or_create(
            content_type=obj_type,
            object_id=obj.id,
            user=user
        )
        if created:
            logger.info("User %s liked object %s:%s", user.id, obj_type.model, obj.id)
        else:
            logger.debug("User %s already liked object %s:%s", user.id, obj_type.model, obj.id)
        
        if like.project is not None:
            like.project.refresh_totals()

    return like


def remove_like(obj: Any, user) -> bool:
    """
    Remove a user like from an object.

    If the user has not liked the object nothing happens so this function can be considered
    idempotent.

    :param obj: Any Django model instance.
    :param user: User removing their like. :class:`~taiga.users.models.User` instance.
    :return: True if like was removed, False if user hadn't liked the object.
    """
    obj_type = apps.get_model("contenttypes", "ContentType").objects.get_for_model(obj)
    with atomic():
        qs = Like.objects.filter(content_type=obj_type, object_id=obj.id, user=user)
        if not qs.exists():
            logger.debug("User %s has not liked object %s:%s", user.id, obj_type.model, obj.id)
            return False

        like = qs.first()
        project = like.project
        qs.delete()
        logger.info("User %s removed like from object %s:%s", user.id, obj_type.model, obj.id)

        if project is not None:
            project.refresh_totals()

    return True


def get_fans(obj: Any) -> QuerySet:
    """
    Get the fans (users who liked) of an object.

    :param obj: Any Django model instance.
    :return: User queryset object representing the users that liked the object.
    """
    obj_type = apps.get_model("contenttypes", "ContentType").objects.get_for_model(obj)
    return get_user_model().objects.filter(likes__content_type=obj_type, likes__object_id=obj.id)


def get_liked(user_or_id: Any, model: Any) -> QuerySet:
    """
    Get the objects liked by a user.

    :param user_or_id: :class:`~taiga.users.models.User` instance or user id.
    :param model: Show only objects of this kind. Can be any Django model class.
    :return: Queryset of objects representing the likes of the user.
    """
    obj_type = apps.get_model("contenttypes", "ContentType").objects.get_for_model(model)
    conditions = (
        'likes_like.content_type_id = %s',
        '%s.id = likes_like.object_id' % model._meta.db_table,
        'likes_like.user_id = %s'
    )

    if isinstance(user_or_id, get_user_model()):
        user_id = user_or_id.id
    else:
        user_id = user_or_id

    return model.objects.extra(
        where=conditions,
        tables=('likes_like',),
        params=(obj_type.id, user_id)
    )
