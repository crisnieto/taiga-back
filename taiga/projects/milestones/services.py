# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2021-present Kaleidos INC

import logging
from typing import List, Dict, Any, Optional

from taiga.base.utils import db
from taiga.events import events
from taiga.projects.history.services import take_snapshot
from taiga.projects.services import apply_order_updates
from taiga.projects.issues.models import Issue
from taiga.projects.tasks.models import Task
from taiga.projects.userstories.models import UserStory

logger = logging.getLogger(__name__)


def calculate_milestone_is_closed(milestone) -> bool:
    """
    Calculate if a milestone should be considered closed.

    A milestone is closed when all its user stories, tasks (without user story),
    and issues are closed.

    :param milestone: Milestone instance to check.
    :return: True if milestone should be closed, False otherwise.
    """
    all_us_closed = all([user_story.is_closed for user_story in
                         milestone.user_stories.all()])
    all_tasks_closed = all([task.status is not None and task.status.is_closed for task in
                            milestone.tasks.filter(user_story__isnull=True)])
    all_issues_closed = all([issue.is_closed for issue in
                             milestone.issues.all()])

    uss_check = (milestone.user_stories.all().count() > 0
        and all_tasks_closed and all_us_closed and all_issues_closed)
    tasks_check = (milestone.tasks.filter(user_story__isnull=True).count() > 0
        and all_tasks_closed and all_issues_closed and all_us_closed)
    issues_check = (milestone.issues.all().count() > 0
        and all_issues_closed and all_tasks_closed and all_us_closed)

    return uss_check or issues_check or tasks_check


def close_milestone(milestone) -> bool:
    """
    Close a milestone if it's not already closed.

    :param milestone: Milestone instance to close.
    :return: True if milestone was closed, False if it was already closed.
    """
    if not milestone.closed:
        milestone.closed = True
        milestone.save(update_fields=["closed",])
        logger.info("Milestone %s closed", milestone.id)
        return True
    return False


def open_milestone(milestone) -> bool:
    """
    Open a milestone if it's currently closed.

    :param milestone: Milestone instance to open.
    :return: True if milestone was opened, False if it was already open.
    """
    if milestone.closed:
        milestone.closed = False
        milestone.save(update_fields=["closed",])
        logger.info("Milestone %s opened", milestone.id)
        return True
    return False


def update_userstories_milestone_in_bulk(bulk_data: List[Dict[str, Any]], milestone: object) -> Dict[int, Any]:
    """
    Update the milestone and the milestone order of some user stories adding
    the extra orders needed to keep consistency.

    :param bulk_data: List of dicts with format [{'us_id': <value>, 'order': <value>}, ...].
    :param milestone: Milestone instance to assign user stories to.
    :return: Dictionary mapping user story IDs to their new sprint orders.
    """
    user_stories = milestone.user_stories.all()
    us_orders = {us.id: getattr(us, "sprint_order") for us in user_stories}
    new_us_orders = {}
    for e in bulk_data:
        new_us_orders[e["us_id"]] = e["order"]
        # The base orders where we apply the new orders must containg all
        # the values
        us_orders[e["us_id"]] = e["order"]

    apply_order_updates(us_orders, new_us_orders)

    us_milestones = {e["us_id"]: milestone.id for e in bulk_data}
    user_story_ids = us_milestones.keys()

    events.emit_event_for_ids(ids=user_story_ids,
                              content_type="userstories.userstory",
                              projectid=milestone.project.pk)

    us_instance_list = []
    us_values = []
    for us_id in user_story_ids:
        us = UserStory.objects.get(pk=us_id)
        us_instance_list.append(us)
        us_values.append({'milestone_id': milestone.id})

    db.update_in_bulk(us_instance_list, us_values)
    db.update_attr_in_bulk_for_ids(us_orders, "sprint_order", UserStory)

    # Updating the milestone for the tasks
    Task.objects.filter(
        user_story_id__in=[e["us_id"] for e in bulk_data]).update(
        milestone=milestone)

    return us_orders


def snapshot_userstories_in_bulk(bulk_data: List[Dict[str, Any]], user) -> None:
    """
    Create snapshots for multiple user stories in bulk.

    :param bulk_data: List of dicts containing 'us_id' keys.
    :param user: User instance creating the snapshots.
    """
    for us_data in bulk_data:
        try:
            us = UserStory.objects.get(pk=us_data['us_id'])
            take_snapshot(us, user=user)
        except UserStory.DoesNotExist:
            logger.warning("User story %s not found for snapshot", us_data.get('us_id'))
            pass


def update_tasks_milestone_in_bulk(bulk_data: List[Dict[str, Any]], milestone: object) -> Dict[int, int]:
    """
    Update the milestone and the milestone order of some tasks adding
    the extra orders needed to keep consistency.

    :param bulk_data: List of dicts with format [{'task_id': <value>, 'order': <value>}, ...].
    :param milestone: Milestone instance to assign tasks to.
    :return: Dictionary mapping task IDs to milestone IDs.
    """
    tasks = milestone.tasks.all()
    task_orders = {task.id: getattr(task, "taskboard_order") for task in tasks}
    new_task_orders = {}
    for e in bulk_data:
        new_task_orders[e["task_id"]] = e["order"]
        # The base orders where we apply the new orders must containg all
        # the values
        task_orders[e["task_id"]] = e["order"]

    apply_order_updates(task_orders, new_task_orders)

    task_milestones = {e["task_id"]: milestone.id for e in bulk_data}
    task_ids = task_milestones.keys()

    events.emit_event_for_ids(ids=task_ids,
                              content_type="tasks.task",
                              projectid=milestone.project.pk)


    task_instance_list = []
    task_values = []
    for task_id in task_ids:
        task = Task.objects.get(pk=task_id)
        task_instance_list.append(task)
        task_values.append({'milestone_id': milestone.id})

    db.update_in_bulk(task_instance_list, task_values)
    db.update_attr_in_bulk_for_ids(task_orders, "taskboard_order", Task)

    return task_milestones


def snapshot_tasks_in_bulk(bulk_data: List[Dict[str, Any]], user) -> None:
    """
    Create snapshots for multiple tasks in bulk.

    :param bulk_data: List of dicts containing 'task_id' keys.
    :param user: User instance creating the snapshots.
    """
    for task_data in bulk_data:
        try:
            task = Task.objects.get(pk=task_data['task_id'])
            take_snapshot(task, user=user)
        except Task.DoesNotExist:
            logger.warning("Task %s not found for snapshot", task_data.get('task_id'))
            pass


def update_issues_milestone_in_bulk(bulk_data: List[Dict[str, Any]], milestone: object) -> Dict[int, int]:
    """
    Update the milestone for some issues.

    :param bulk_data: List of dicts with format [{'issue_id': <value>}, ...].
    :param milestone: Milestone instance to assign issues to.
    :return: Dictionary mapping issue IDs to milestone IDs.
    """
    issue_milestones = {e["issue_id"]: milestone.id for e in bulk_data}
    issue_ids = issue_milestones.keys()

    events.emit_event_for_ids(ids=issue_ids,
                              content_type="issues.issues",
                              projectid=milestone.project.pk)

    issues_instance_list = []
    issues_values = []
    for issue_id in issue_ids:
        issue = Issue.objects.get(pk=issue_id)
        issues_instance_list.append(issue)
        issues_values.append({'milestone_id': milestone.id})

    db.update_in_bulk(issues_instance_list, issues_values)

    return issue_milestones


def snapshot_issues_in_bulk(bulk_data: List[Dict[str, Any]], user) -> None:
    """
    Create snapshots for multiple issues in bulk.

    :param bulk_data: List of dicts containing 'issue_id' keys.
    :param user: User instance creating the snapshots.
    """
    for issue_data in bulk_data:
        try:
            issue = Issue.objects.get(pk=issue_data['issue_id'])
            take_snapshot(issue, user=user)
        except Issue.DoesNotExist:
            logger.warning("Issue %s not found for snapshot", issue_data.get('issue_id'))
            pass
