#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""This Telegram bot helps users share current availability and approximate location
with members of the same organization. The users must also be within a specified distance.
Sample uses include group or 1 on 1 meetings between people with the same interests.

This Bot is a Google Cloud function. It also uses Google Cloud Datastore.

This program is dedicated to the public domain under the MIT License.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime

from telegram import Bot, ChatAction, Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, \
    ReplyKeyboardMarkup, ReplyKeyboardRemove

from google.cloud import datastore

from geopy import distance

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Define global vars and constants
bot = Bot(token=os.environ["TELEGRAM_TOKEN"])
db = datastore.Client()

# A dictionary of lists holding organizations as keys and members belonging to them as values (lists).
# A user can be in several orgs
organizations = defaultdict(list)

# A dictionary holding members as keys and their preferences as values.
# members['pytonist'] =
# {'selected_org': 'Pythonists', 'travel_radius': '3', 'location': {'longitude': 37.742, 'latitude': 55.45}}
members = {}


def db_query_by_kind(kind):
    """Query the datastore by entity kind (e.g. Organization, Member). The result is a list of Entities.
    To get an entity's id do result[index].id. If the id is custom, do result[index].key.name
    To get entity's items do result[index].items(). It can be converted to list result[index]['members'].
    Args:
        kind: GCP datastore entity kind.
    Returns:
        A list of entities of the specified kind.
    """
    logger.info("In db_query_by_kind handler.")
    query = db.query(kind=kind)
    # query.add_filter('priority', '>=', 4)
    # query.order = ['-priority']
    return list(query.fetch())


def db_get_entity(kind, entity_name):
    """Get entity from the datastore
    Args:
        kind: GCP datastore entity kind.
        entity_name: entity name to find.
    Returns:
        The found entity if any.
    """
    logger.info("In db_get_entity handler.")
    key = db.key(kind, entity_name)
    task = db.get(key)
    return task


def db_batch_lookup(kind, entity_names):
    """Get multiple entities from the datastore
    Args:
        kind: GCP datastore entity kind.
        entity_names: entity names to find.
    Returns:
        The found entities if any.
    """
    logger.info("In db_batch_lookup handler.")
    keys = [db.key(kind, entity_name) for entity_name in entity_names]
    tasks = db.get_multi(keys)
    return tasks


def db_upsert_org(org_cd, org_members):
    """Upsert the specified organization.
    Args:
        org_cd: organization name to upsert.
        org_members: values to assign to the 'members' property .
    Returns:
        The created/updated organization entity.
    """
    logger.info("In db_upsert_org handler.")
    task = datastore.Entity(db.key('Organization', org_cd), exclude_from_indexes=('members',))
    task.update({'members': org_members})
    db.put(task)
    return task


def db_upsert_member(username, selected_org, travel_radius, location):
    """Upsert the specified member.
    Args:
        username: member name to upsert.
        selected_org: value to assign to the 'selected_org' property .
        travel_radius: value to assign to the 'travel_radius' property .
        location: value to assign to the 'location' property .
    Returns:
        The created/updated member entity.
    """
    logger.info("In db_upsert_member handler.")
    task = datastore.Entity(db.key('Member', username), exclude_from_indexes=('travel_radius', 'location',))
    task.update(
        {
            'selected_org': selected_org,
            'travel_radius': travel_radius,
            'location': datastore.helpers.GeoPoint(location['latitude'], location['longitude']),
            'created_dttm': datetime.utcnow(),
        }
    )
    db.put(task)
    return task


def refresh_members():
    """Delete members from the datastore that have not updated their location today.
    Populate local members dictionary with current info from the datastore.
    Returns:
        None; output is written to Stackdriver Logging.
    """
    logger.info("In refresh_members handler.")
    global members
    members = {}
    member_entities = db_query_by_kind('Member')
    current_datetime = datetime.utcnow()
    entity_keys_for_deletion = []
    for member_entity in member_entities:
        if member_entity['created_dttm'].day == current_datetime.day:
            members[member_entity.key.name] = {}
            members[member_entity.key.name]['selected_org'] = member_entity['selected_org']
            members[member_entity.key.name]['travel_radius'] = member_entity['travel_radius']
            members[member_entity.key.name]['location'] = {
                'longitude': member_entity['location'].to_protobuf().longitude,
                'latitude': member_entity['location'].to_protobuf().latitude,
            }
        else:
            # Add records from previous day or older for deletion
            entity_keys_for_deletion.append(db.key('Member', member_entity.key.name))

    if len(entity_keys_for_deletion) > 0:
        db.delete_multi(entity_keys_for_deletion)


def refresh_organizations():
    """Ensure only authorized orgs are in the datastore. Sync the organizations dictionary of the current instance
    with the datastore to populate members. If an organization is authorized,
    but is not in the datastore (has no members yet), add it to the organizations dictionary of the instance.
    Returns:
        None; output is written to Stackdriver Logging.
    """
    logger.info("In update_organizations_from_db handler.")

    global organizations
    organizations = {}
    organizations = defaultdict(list)

    # Orgs currently authorized to use the bot. Uppercase the string first and then split into an array
    authorized_orgs = os.environ["AUTHORIZED_ORGS"].upper().split(',')
    organization_entities = db_query_by_kind('Organization')
    entity_keys_for_deletion = []
    for org_entity in organization_entities:
        if org_entity.key.name in authorized_orgs:
            organizations[org_entity.key.name] = org_entity['members']
        else:
            # Add no longer authorized orgs for deletion
            entity_keys_for_deletion.append(db.key('Organization', org_entity.key.name))

    if len(entity_keys_for_deletion) > 0:
        db.delete_multi(entity_keys_for_deletion)

    # Ensure all the authorized orgs are in the organizations variable.
    # As soon as a member joins a new org, the datastore will be updated and the org will be in the datastore too.
    if len(organizations) < len(authorized_orgs):
        for org_code in authorized_orgs:
            if org_code not in organizations:
                organizations[org_code] = []


def add_new_user(update):
    """Add current user to their selected organization if it is an authorized one.
    Args:
        update: an incoming Telegram update.
    Returns:
        None; output is written to Stackdriver Logging.
    """
    logger.info("In addNewUser handler.")
    # Check whether the user entered a correct name of an organization
    selected_org = update.message.text.upper()
    username = update.message.from_user.username
    if not username:
        update.message.reply_text('Users must have a username to use this bot. Please update your telegram'
                                  ' profile and retry. To get help use /help command. To start again when ready use'
                                  ' /start.')
        return
    if selected_org in organizations:
        logger.info("Adding current user to the organizations dictionary and the datastore")
        # Adding current user to the organizations dictionary and the datastore
        organizations[selected_org].append(username)
        db_upsert_org(selected_org, organizations[selected_org])
        # Adding current user to the members dictionary
        update_daily_active_user(username, selected_org=selected_org)
        update.message.reply_text('You were added to the organization ' + selected_org)
        build_distance_selector(update, selected_org)
    else:
        # Prompt user to enter their org name
        update.message.reply_text('Hi! Please enter the name of your organization or group:')


def update_daily_active_user(username, selected_org=None, travel_radius=None, location=None):
    """Update the members dictionary
    Args:
        username:  Required
        selected_org: Optional
        travel_radius: Optional
        location: Optional
    Returns:
        None; output is written to Stackdriver Logging.
    """
    if username:
        if username not in members:
            members[username] = {}
        if selected_org:
            members[username]['selected_org'] = selected_org
        if travel_radius:
            members[username]['travel_radius'] = travel_radius
        if location:
            members[username]['location'] = location
    else:
        logger.error('Required field "username" is missing')

    if 'selected_org' in members[username] and 'travel_radius' in members[username] and 'location' in members[username]:
        # Adding current user to the members datastore
        db_upsert_member(
            username,
            members[username]['selected_org'],
            members[username]['travel_radius'],
            members[username]['location']
        )


def build_distance_selector(update, selected_org):
    """Build the keyboard for the distance selector.
    Args:
        update: an incoming Telegram update.
        selected_org: the org selected by the current user.
    Returns:
        None; output is written to Stackdriver Logging.
    """

    # TODO: check if the radius is already defined in members[username]['travel_radius']
    # if so, let the user know the value and offer to continue, change radius, or change the selected org.
    logger.info("In buildDistanceSelector handler")
    keyboard = [
        [
            InlineKeyboardButton("1 km", callback_data='1'),
            InlineKeyboardButton("2 km", callback_data='2'),
        ],
        [
            InlineKeyboardButton("3 km", callback_data='3'),
            InlineKeyboardButton("4 km", callback_data='4'),
        ],
        # TODO: In the future support multiple orgs per user and allow to switch the selected org
        # [
        #   InlineKeyboardButton("Change organization", callback_data='change_org'),
        # [
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        update.message.reply_text(
            "{} is your currently selected organization. "
            "Please choose how far you are willing to travel:".format(selected_org),
            reply_markup=reply_markup,
        )


def inline_keyboard_handler(update):
    """Method to handle user selections on the inline keyboard.
    Args:
        update: an incoming Telegram update.
    Returns:
        None; output is written to Stackdriver Logging.
    """
    logger.info("In button handler. Query data: " + str(update.callback_query.data))
    query = update.callback_query

    if query.data.isdigit():
        update_daily_active_user(query.from_user.username, travel_radius=query.data)
        bot.edit_message_text(text="You selected {} km search radius".format(query.data),
                              chat_id=query.message.chat_id,
                              message_id=query.message.message_id)
        bot.answer_callback_query(query.id)
        request_location(update)
    else:
        logger.warning('Update "%s" caused error. Expected a digit, received "%s"', update, query.data)


def request_location(update):
    """Request user location.
    Args:
        update: an incoming Telegram update.
    Returns:
        None; output is written to Stackdriver Logging.
    """
    logger.info("In request_location.")
    location_keyboard = KeyboardButton(text="Send Location", request_location=True)
    custom_keyboard = [[location_keyboard]]
    reply_markup = ReplyKeyboardMarkup(custom_keyboard)
    bot.send_message(
        chat_id=update.callback_query.message.chat_id,
        text="Please share your location with me, so I can find the other org members nearby",
        reply_markup=reply_markup)


def check_who_is_around(update):
    """Check if other members are within the specified distance.
    Args:
        update: an incoming Telegram update.
    Returns:
        None; output is written to Stackdriver Logging.
    """
    logger.info("In check_who_is_around")
    current_username = update.message.from_user.username
    current_user = members[current_username]
    logger.info('Current user keys: ' + ', '.join(current_user.keys()))
    selected_org = current_user['selected_org']
    usernames_in_the_org = organizations[selected_org]
    users_nearby = []

    for username in usernames_in_the_org:
        if username != current_username\
                and username in members\
                and compute_distance(current_user['location'], members[username]['location']) <= \
                float(current_user['travel_radius']):
            logger.info(username + 'is nearby')
            users_nearby.append('@' + username)

    reply_markup = ReplyKeyboardRemove()
    if len(users_nearby) > 0:
        update.message.reply_text(
            'The following members are near you {}'.format(', '.join(users_nearby)),
            reply_markup=reply_markup,
        )
        # TODO: Here offer to send a group chat "Would you like to meet in an hour at ...
    else:
        update.message.reply_text(
            'Sorry, no one is around at this time. To get help use /help command. To start again use /start.',
            reply_markup=reply_markup,
        )


def compute_distance(location_a, location_b):
    """Computes distance between 2 locations in kilometers.
    Args:
        location_a: GPS coordinates of the first location, e.g. {'longitude': 37.742, 'latitude': 55.45}
        location_b: GPS coordinates of the second location, e.g. {'longitude': 33.742, 'latitude': 55.45}
    Returns:
        The distance between the locations in kilometers.
    """
    logger.info("In compute_distance.")

    loc_a = (location_a['longitude'], location_a['latitude'])
    loc_b = (location_b['longitude'], location_b['latitude'])

    return distance.distance(loc_a, loc_b).km


def log_warning(update, warn_message):
    """Log warnings caused by Updates.
    Args:
        update: an incoming Telegram update to log.
        warn_message: the warning message to log.
    Returns:
        None; output is written to Stackdriver Logging
    """
    logger.info("In error handler")
    logger.warning('Update "%s" caused warning "%s"', update, warn_message)


def log_error(update, error_message):
    """Log errors caused by Updates.
    Args:
        update: an incoming Telegram update to log.
        error_message: the error message to log.
    Returns:
        None; output is written to Stackdriver Logging
    """
    logger.info("In error handler")
    logger.error('Update "%s" caused error "%s"', update, error_message)


def get_message_age(message):
    """Compute the time since the last message first arrived.
    Args:
        message: a message from the Telegram update.
    Returns:
        message age in milliseconds.
    """
    event_time = message.date
    if message.edit_date:
        event_time = message.edit_date
    event_age = (datetime.now() - event_time).total_seconds()
    event_age_ms = event_age * 1000
    logger.info(str(event_age_ms))
    return event_age_ms


def timeout(update, message):
    """Check whether it has not been too long since the last message.
    Helpful to avoid handling the same request that cannot be handled
    because of a bug in the code.
    Args:
        update: an incoming Telegram update.
        message: a message from the Telegram update.
    Returns:
        True or False.
    """
    event_age_ms = get_message_age(message)
    # Ignore events that are too old
    max_age_ms = 10000
    if event_age_ms < max_age_ms:
        return False
    else:
        print('Dropped {} (age {}ms)'.format(update.update_id, event_age_ms))
        return True


def bot_help(update):
    """Send a message when the command /help is issued.
    Args:
        update: an incoming Telegram update.
    Returns:
        None; The output is written to Stackdriver Logging
    """
    logger.info("In help handler")
    # TODO: Add instructions for delete user's data etc.
    update.message.reply_text('Please use /start command to start or restart the bot.\n' 
                              'We store the location information that you submitted for 24 hours maximum.\n'
                              'If you would like to add your organization to We Meet Bot as a private one and use '
                              'the bot for your needs, please contact @tigmir. Prices for private(closed) organizations'
                              ' start at $5 per month per organization.\n' 
                              'If you have any additional questions, please contact @tigmir.')


def start(update):
    """Start and restart handler. Checks what additional information is necessary and acts accordingly.
    Args:
        update: an incoming Telegram update.
    Returns:
        None; The output is written to Stackdriver Logging
    """
    logger.info("In start handler.")

    refresh_organizations()
    refresh_members()

    # Check whether the user is already in the organization. One org for now.
    # A different approach later, as loop is not practical when many orgs.
    username = update.message.from_user.username
    # TODO: support multiple orgs instead of only the first one.
    #  Below we need to find the first org, where this username is found. So, in the future, we will search among all
    #  orgs. At present we have only one org {org_code}. Therefore, no need to loop among all.
    org_code = list(organizations.keys())[0]
    if username in members:
        if members[username]['selected_org']:
            build_distance_selector(update, members[username]['selected_org'])
        else:
            logger.warning("Probably an error. User exists in the user dictionary, but without an org. " +
                           "Calling add_new_user() to rewrite")
            add_new_user(update)
    elif username in organizations[org_code]:
        update_daily_active_user(username, selected_org=org_code)
        build_distance_selector(update, org_code)
    else:
        add_new_user(update)


def webhook(request):
    """Webhook for the telegram bot. This Cloud Function only executes within a certain
    time period after the triggering event.
    Args:
        request: A flask.Request object. <http://flask.pocoo.org/docs/1.0/api/#flask.Request>
    Returns:
        Response text.
    """

    logger.info("In webhook handler")

    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)

        # your bot can receive updates without messages
        if update.message:
            if timeout(update, update.message):
                return "Timeout"
            bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)
            # I need a switch() here!!
            if update.message.text == "/start":
                start(update)
                return "ok"
            if update.message.text == "/help":
                bot_help(update)
                return "ok"
            if update.message.location:
                update_daily_active_user(update.message.from_user.username, location=update.message.location)
                check_who_is_around(update)
                return "ok"

            # default
            add_new_user(update)
            return "ok"

        # Handle user inline keyboard events
        if update.callback_query:
            if timeout(update, update.callback_query.message):
                return "Timeout"
            bot.send_chat_action(chat_id=update.callback_query.message.chat_id, action=ChatAction.TYPING)
            inline_keyboard_handler(update)
            return "ok"

        return "error"
    else:
        # Only POST accepted
        logger.warning("Only POST method accepted")
        return "error"

