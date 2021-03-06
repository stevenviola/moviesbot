import webapp2
import logging
import datetime
import yaml
import config
import re
import json
import textwrap
import traceback

from protorpc import messages
from protorpc import message_types

from google.appengine.ext import ndb
from google.appengine.api import taskqueue
from google.appengine.ext.ndb import msgprop

from modules.reddit import Reddit
from modules.imdb import IMDB
from modules.mediahound import MediaHound
from modules import parse_text_for_imdb_ids, parse_text_for_rt_ids, rotten_tomatoes_2_imdb

from modules.models import Movies, MovieTypes, Post, Comment, CommentRevisions, IgnoreList, Whitelisted, Blacklisted

REDDIT_PM_IGNORE   = "http://www.reddit.com/message/compose/?to={username}&subject=IGNORE%20ME&message=[IGNORE%20ME](http://i.imgur.com/s2jMqQN.jpg\)".format(username=config.reddit['user'])
REDDIT_PM_REMEMBER = "http://www.reddit.com/message/compose/?to={username}&subject=REMEMBER%20ME&message=I%20made%20a%20mistake%20I%27m%20sorry,%20will%20you%20take%20me%20back".format(username=config.reddit['user'])
REDDIT_PM_DELETE   = "http://reddit.com/message/compose/?to={username}&subject=delete&message=delete%20{thing_id}".format(username=config.reddit['user'],thing_id='{thing_id}')
REDDIT_PM_FEEDBACK = "https://docs.google.com/forms/d/1PZTwDM71_Wiwxdq6NGKHI1zf-GC2oahqxwn8tX-Hq_E/viewform"
REDDIT_PM_MODS     = "https://www.reddit.com/r/{subreddit}/wiki/faq#wiki_info_for_moderators".format(subreddit=config.subreddit)
REDDIT_FAQ         = "https://www.reddit.com/r/{subreddit}/wiki/faq".format(subreddit=config.subreddit)
REDDIT_MAINTAINER  = "\/u/stevenviola"
MAINTAINER_LINK    = "http://www.reddit.com/message/compose/?to=stevenviola"
SOURCE_CODE        = "https://github.com/stevenviola/moviesbot"
NO_BREAK_SPACE = u'&nbsp;'
MAX_MESSAGE_LENGTH = 10000

SIG_LINKS = [
    '[](#bot)',
    '[Stop%sReplying](%s)' % (NO_BREAK_SPACE, REDDIT_PM_IGNORE),
    '[Delete](%s)' % REDDIT_PM_DELETE,
    '[FAQ](%s)' % REDDIT_FAQ,
    '[Source](%s)' % SOURCE_CODE,
    ('Created{s}and{s}maintained{s}by{s}[{maintainer}]({maintainer_link})').format(
        s=NO_BREAK_SPACE,
        maintainer=REDDIT_MAINTAINER,
        maintainer_link=MAINTAINER_LINK
    ),
    '[](#bot)'
]

class PostObject:
    def __init__(self,post_id,post=None):
        self.post_id = post_id
        self.movies_list = []
        post_key = self.get_post_key()
        if post_key:
            logging.info("Data already in DB. Populating the object from DB")
            # This post is already in the DB
            self.populate_data()
        else:
            if not post:
                logging.debug("Post data was not provided and not in DB. Need to lookup in DB")
                # Not in DB and no post_data provided. Need to make API call to get the info
                post_results = reddit.api_call("https://oauth.reddit.com/api/info.json?id=%s" % post_id)
                if post_results:
                    logging.info("Processing for post: %s" % post_id)
                    post = post_results['data']['children'][0]
                else:
                    logging.error("Unable to get results for post %s" % post_id)
                    # Throw error to get out of here
            else:
                logging.debug("Post data provided. Skipping another API request")
                logging.debug(post)
            if 'kind' in post:
                self.kind = post['kind']
            elif 'kind' in post['data']:
                self.kind = post['data']
            else:
                logging.error("This post has no kind")
                logging.debug(post_results)
                # Throw an issue. a post needs a kind
            self.author    = post['data']['author']
            self.post_date = datetime.datetime.fromtimestamp(int(post['data']['created_utc']))
            self.subreddit = post['data']['subreddit']
            self.name      = post['data']['name']
            self.commented   = False
            self.processing  = False
            self.link_sources = {}
            if self.kind == 't3':
                # this is a post
                self.permalink = post['data']['permalink']
                self.link_sources['selftext'] = post['data']['selftext']
                self.link_sources['url'] = post['data']['url']
                self.link_sources['title'] = post['data']['title']
            elif self.kind == 't1':
                # This is a comment
                self.permalink = None
                self.link_sources['body'] = post['data']['body']
            logging.info("Need to search the link_sources for IMDB links")
            for link_source in self.link_sources:
                logging.debug(link_source)
                self.movies_list += parse_text_for_imdb_ids(self.link_sources[link_source])
                #self.movies_list += rotten_tomatoes_2_imdb(parse_text_for_rt_ids(self.link_sources[link_source]))
            # Cast the list to a set, and then back to a list to get unique movie ids
            self.movies_list = list(set(self.movies_list))
            self.movies = []
            for movie in self.movies_list:
                self.movies.append(ndb.Key(Movies, movie))
            logging.debug(self.movies_list)
            logging.info("Post of kind %s had id of %s, submitted on %s to the %s subreddit by %s." % (
                self.kind,
                self.name,
                self.post_date,
                self.subreddit,
                self.author
            ))
            self.add_post_to_db()

    def is_comment(self):
        if self.kind == 't1':
            return True
        else:
            return False
    # Adds the post to the database
    # Returns the key for the post in the DB
    def add_post_to_db(self):
        logging.debug("Adding %s to the datastore now" % self.name)
        post_key = Post(
            id          = self.name,
            post_kind   = self.kind,
            name        = self.name,
            movies      = self.movies,
            movies_list = self.movies_list,
            post_date   = self.post_date,
            author      = self.author,
            permalink   = self.permalink,
            subreddit   = self.subreddit
        ).put()

    def populate_data(self):
        post_key = self.get_post_key()
        if post_key:
            self.kind        = post_key.post_kind
            self.movies_list = post_key.movies_list
            self.post_date   = post_key.post_date
            self.name        = post_key.name
            self.author      = post_key.author
            self.permalink   = post_key.permalink
            self.subreddit   = post_key.subreddit
            self.commented   = post_key.commented
            self.processing  = post_key.processing 
            logging.debug("Got back %s from NDB, so setting self.movies_list to %s" % (post_key.movies,self.movies_list))
            logging.debug("Got back %s from NDB, so setting self.author to %s" % (post_key.author,self.author))
            logging.debug("Got back %s from NDB, so setting self.subreddit to %s" % (post_key.subreddit,self.subreddit))
        else:
            logging.error("Post Key not found. Can not update anything")

    def get_post_key(self):
        key = ndb.Key(Post, self.post_id).get()
        if key:
            logging.debug("Post key in DB")
            logging.debug(key)
            return key
        else:
            logging.debug("Post key is not in the DB")
            return None

    def add_comment_to_post(self,comment_id,body):
        post_key = self.get_post_key()
        if post_key:
            comment_key = Comment(
                id = comment_id,
                parent = post_key.key,
                name = comment_id,
                score = 1,
                revision = 0
            ).put()
            comment_rev_key = CommentRevisions(
                id = '0',
                parent = comment_key,
                body = body
            ).put()
            post_key.commented = True
            post_key.put()
            # Repopulate the data from the DB
            self.populate_data()
        else:
            logging.error("Post Key not found. Can not update with comment")

    def set_processing(self,processing):
        post_key = self.get_post_key()
        if post_key:
            post_key.processing = processing
            post_key.put()
            # Repopulate the data from the DB
            self.populate_data()
        else:
            logging.error("Post Key not found. Can not set processing")

def is_author_ignored(author):
    author_ignored = IgnoreList.query(ndb.AND(
        IgnoreList.author == author,
        IgnoreList.ignored == True
    )).fetch()
    if not author_ignored:
        return False
    else:
        return True

def author_ignore_key(author):
    author_ignored = IgnoreList.query(IgnoreList.author == author).get()
    if not author_ignored:
        return False
    else:
        return author_ignored

def is_listed(list_type,subreddit):
    logging.debug("Checking to see if %s is %slisted" % (subreddit,list_type))
    if list_type is 'white':
        entity = Whitelisted
    elif list_type is 'black':
        entity = Blacklisted
    else:
        return False
    if entity:
        listed = entity.query(entity.subreddit == subreddit).get()
        if listed:
            logging.debug("%s is %slisted. Returning True" % (subreddit,list_type))
            return True
    return False

def uniform_types(method_type):
    ret = method_type
    if method_type == 'broker':
        ret ='subscription'
    elif method_type == 'rental':
        ret ='rent'
    elif method_type == 'adSupported':
        ret ='subscription'
    return ret.title()

def sort_method_types(method_types):
    ret = []
    # These are method types we care about
    ordered_types = ['Subscription', 'Rent', 'Purchase']
    for i in ordered_types:
        if i in method_types:
            ret.append(i.title())
    # Append any unmatched methods:
    unmatched = [x.title() for x in method_types if x not in ordered_types]
    if unmatched:
        logging.debug(unmatched)
        ret.extend(list(set(unmatched)))
    return ret

def lookup_movie_data(movies):
    for imdb_id in movies:
        imdb_obj = IMDB(imdb_id)
        if not imdb_obj.movie_data.mhid:
            mh_imdb_id = "IMDB::%s" % imdb_id
            mhid = mh.graph_enter([mh_imdb_id])[mh_imdb_id]
        else:
            mhid = imdb_obj.movie_data.mhid
        logging.debug("MediaHound ID is: %s" % mhid)
        if mhid is None:
            continue
        if imdb_obj.movie_data.mhid is None:
            mh_metadata = mh.graph_media(mhid)
            movie_metadata = {
                'mhid'     : mhid,
                'mh_name'  : mh_metadata['metadata']['name'],
                'mh_altId' : mh_metadata['metadata']['altId']
            }
            imdb_obj.add_metadata(movie_metadata)
        


"""
Takes a list of IMDB ids and returns array of dictionaries
with the information about each movie
"""
def get_movie_data(movies):
    if not movies:
        return False
    media_types = config.mediatypes
    movies_ret = {}
    movies_ret['movies'] = []
    movies_ret['friendly_names'] = []
    movies_ret['media_types'] = []
    for imdb_id in movies:
        logging.debug("Looking up information for IMDB id: %s" %imdb_id)
        movie_obj = {}
        # Lookup IMDB name
        imdb_obj = IMDB(imdb_id)
        if imdb_obj.movie_data.Type != MovieTypes.movie:
            logging.info("Skipping non movie link: %s. Type is: %s" %
                (imdb_id, imdb_obj.movie_data.Type)
            )
            continue
        imdb_title = imdb_obj.movie_data.Title
        imdb_release = imdb_obj.movie_data.DVD
        if imdb_release and datetime.datetime.now() < imdb_release:
            logging.info("Looks like the DVD hasn't come out yet. Perhaps we should not include this movie") 
        if not imdb_title:
            logging.warning("Couldn't get IMDB info for IMDB id: %s" %imdb_id)
            continue
        movie_obj = {}
        movie_obj['imdb_rating'] = imdb_obj.movie_data.imdbRating
        movie_obj['imdb_id'] = imdb_id
        movie_obj['imdb_title'] = imdb_title
        movie_obj['tomatoMeter'] = imdb_obj.movie_data.tomatoMeter
        movie_obj['rottentomatoes'] = imdb_obj.movie_data.tomatoURL
        movie_obj['media_types'] = {}
        movie_obj['exclude'] = True
        if imdb_obj.movie_data.mhid:
            movie_obj['mhid'] = imdb_obj.movie_data.mhid
            movie_obj['mh_title'] = imdb_obj.movie_data.mh_name
            movie_obj['mh_altId'] = imdb_obj.movie_data.mh_altId
            mh_sources = mh.graph_media(imdb_obj.movie_data.mhid,'sources')
            movie_obj['exclude'] = not mh_sources['content']
            for mh_object in mh_sources['content']:
                if 'allMediums' in mh_object['object'] and mh_object['object']['allMediums']:
                    movies_ret['friendly_names'].extend(mh_object['object']['allMediums'])
                    media_provider = mh_object['object']['metadata']['name']
                    logging.debug("Found media from: %s" % media_provider)
                    for medium in mh_object['context']['mediums']:
                        for method in medium['methods']:
                            method_type = uniform_types(method['type'])
                            logging.debug("Type is %s" % method['type'])
                            movies_ret['media_types'].append(method_type)
                            if method_type not in movie_obj['media_types']:
                                movie_obj['media_types'][method_type] = {}
                            for format in method['formats']:
                                url = format['launchInfo']['view']['http']
                                if 'price' in format:
                                    price = format['price']
                                else:
                                    price = 0
                                if media_provider not in movie_obj['media_types'][method_type] or price < movie_obj['media_types'][method_type][media_provider]['price']:
                                    movie_obj['media_types'][method_type][media_provider] = {
                                        'url'  : url,
                                        'price': price
                                    }
        movies_ret['movies'].append(movie_obj)
    movies_ret['friendly_names'] = list(set(movies_ret['friendly_names']))
    movies_ret['media_types'] = sort_method_types(movies_ret['media_types'])
    logging.debug(movies_ret)
    # Return Object
    return movies_ret

"""
Given a post, determines if we should comment
- Returns True if we should comment
- Returns False if we shouldn't comment on post
"""

def should_comment(post,forced=False,summoned=False):
    # If forced, return true
    if forced is True:
        logging.info("Forced is true. I don't care about anything else. Should comment")
        return True
    # If summoned and subreddit isn't blacklisted, return True
    elif summoned is True and is_listed('black',post.subreddit) is False:
        logging.info("I was summoned and the subreddit is not blacklisted. Should comment")
        return True
    # If user is on ignore list, return false
    elif is_author_ignored(post.author):
        logging.info("Author is on the ignore list. Should not comment")
        return False
    # If subreddit is on whitelist, return true
    elif is_listed('white',post.subreddit) is True:
        logging.info("Subreddit is on the whitelist. Should comment")
        return True
    else:
        logging.info("Subreddit isn't on the whitelist. Should not comment")
        return False

"""
Given a post and movies data, need to do the following:
- format comment
- reply to the post
"""
def comment_on_post(post, summoned=False):
    name = post.name
    movies_list = post.movies_list
    # Set this post to processing
    logging.debug("Setting processing to True")
    post.set_processing(True)
    try:
        # If we got valid movie data back
        if movies_list is not None:
            logging.info(movies_list)
            # We should comment on this post
            movies_data = get_movie_data(movies_list)
            if len(movies_data['movies']) > 0:
                comment_text = format_new_post(movies_data)
                # If the comment text has info
                if comment_text is not False and ( len(movies_data['media_types']) > 0 or summoned is True ):
                    submit_comment(post,comment_text)
                elif summoned is True:
                    logging.critical("This condition shouldn't happen. Investigate why this was called")
                    comment_text = "Sorry, I couldn't find any links to streaming, rental, or purchase sites. Perhaps the movie is too new\n"
                    submit_comment(post,comment_text)
                else:
                    logging.info("No links to provide to the user, and not summoned. Not commenting")
            elif summoned is True:
                logging.info("No movie data was found for post but I was summoned. Need to update with sad comment")
                comment_text = "Sorry, I was unable to find any movies in this post\n"
                submit_comment(post,comment_text)
            else:
                logging.info("No movie data and not summoned. Not commenting")
        else:
            logging.debug("No movies to comment on. Reply skipping.")
    except Exception, e:
        logging.critical("Encountered error when processing post. Abort: %s" % traceback.print_exc());
    # Unset processing
    post.set_processing(False)
    logging.debug("Processing set to False")

def pm_summon(message):
    missing_link_error = "I can't find a valid reddit link in the message body"
    author  = message['author']
    body    = message['body']
    date    = datetime.datetime.fromtimestamp(int(message['created_utc']))
    subject = message['subject'].lower()
    message_id = int(message['id'],36)
    # Parse out a link to a post
    match = re.search(
        r'r\/[\w]+\/comments\/(?P<post_id>[a-z0-9]+)(\/[\w]+\/)?(?P<comment_id>[a-z0-9]+)?',
        body
    )
    if not match:
        return missing_link_error
    post_id = match.group('post_id')
    comment_id = match.group('comment_id')
    if comment_id is not None:
        post = PostObject('t1_%s' % comment_id)
    elif post_id is not None:
        post = PostObject('t3_%s' % post_id)
    else:
        return missing_link_error
    logging.info("Subreddit is %s" % post.subreddit)
    # Check to see if user is moderator of subreddit
    # If not, abort now
    if not reddit.is_user_moderator(post.subreddit,author):
        return "Sorry, this feature is only available to moderators of %s" % post.subreddit
    if not should_comment(post):
        return "I don't think I should comment on this post. Either because the user has requested I not respond to them, or because the subreddit is on the blacklist"
    movies_list = parse_text_for_imdb_ids(body)
    if movies_list is None:
        return "Couldn't find any IMDB links in your message"
    if post.processing:
        return "This post is currently processing. Try back in a few minutes"
    lookup_movie_data(movies_list)
    movies_data = get_movie_data(movies_list)
    logging.debug(movies_data)
    if movies_data is False or len(movies_data['movies']) == 0:
        return "Couldn't find any movies in your message"
    comment_text = format_new_post(movies_data)
    submit_comment(post,comment_text)
    return "Hooray! that comment has been posted for you"

"""
Replies to a post with the comment text provided
Adds the reply to the DB and edits the comment for the delete button
"""
def submit_comment(post,comment_text):
    name = post.name
    footer = '\n---\n' + ' ^| '.join(['^' + a for a in SIG_LINKS])
    comment_text += footer
    new_post_result =  reddit.post_to_reddit(name,comment_text,'comment')
    # If the comment was posted sucessfully
    if new_post_result:
        if not new_post_result['json']['errors']:
            # get the name of the comment
            comment_name = new_post_result['json']['data']['things'][0]['data']['name']
            logging.info("Adding to the db. Will not comment on this post again")
            post.add_comment_to_post(comment_name,comment_text)
            update_comment(name,comment_name,comment_text)
        else:
            # Set comment id to 0 and let this get put in the DB, so we don't try it again
            logging.error("Received the following error when trying to comment: %s" % new_post_result['json']['errors'])
    else:
        logging.error("Couldn't comment. Not marking this as commented in DB")

def update_comment(post_id,comment_id,body):
    comment_key = ndb.Key(Post, post_id, Comment, comment_id)
    comment = comment_key.get()
    rev = comment.revision+1;
    updated_comment_text = body.format(thing_id=comment_id)
    reddit.post_to_reddit(comment_id,updated_comment_text,'editusertext')
    comment_rev_key = CommentRevisions(
        id = str(rev),
        parent = comment_key,
        body = updated_comment_text
    ).put()
    comment.revision = rev
    comment.put()

def format_new_post(movies_data):
    media_types = movies_data['media_types']
    friendly_names = movies_data['friendly_names']
    pulral = ''
    if len(movies_data['movies']) > 1:
        pulral = 's'
    if len(friendly_names) == 0:
        ret_line = ["Sorry, no streaming, rental, or purchase links found for the following movies:\n\n"]
    else:
        ret_line = [
            "Here's where you can %s the movie%s listed:\n\n" %
                ('/'.join(friendly_names),pulral)
        ]
    heading = ['Title','IMDB','Rotten Tomatoes']
    heading += media_types
    seperator = []
    actual_links = False
    for index, w in enumerate(heading):
        sep = "---"
        if index > 1:
            sep+=":"
        seperator.append(sep)
    ret_line.append(" | ".join(heading))
    ret_line.append("|".join(seperator))
    for movie in movies_data['movies']:
        # If we have details about the movie, but no 
        # links, then just add a message
        if movie['exclude'] and 'imdb_title' not in movie:
            logging.info("We don't have any info, so tell the user we excluded the title")
#            line.append("No %s options for: %s" % ( ' , '.join(media_types), title ))
            continue
        actual_links = True
        rt_rating = movie['tomatoMeter']
        if rt_rating is None:
            rt_rating = 'N/A'
        else:
            rt_rating = "{0}%".format(rt_rating)
        rt_link = movie['rottentomatoes']
        imdb_rating = movie['imdb_rating']
        if imdb_rating is None:
            imdb_rating = 'N/A'
        imdb_link = "http://www.imdb.com/title/%s/" % movie['imdb_id']
        if 'mhid' in movie:
            short_url = "https://nextqueue.com/movie/%s" % movie['mh_altId'][6:]
            title = movie['mh_title']
        else:
            short_url = imdb_link
            title = movie['imdb_title']
        line = ["**[%s](%s)**" % (title, short_url)]
        line.append("[%s](%s)" % (imdb_rating,imdb_link))
        if rt_link is not None:
            line.append("[{0}]({1})".format(rt_rating,rt_link))
        else:
            line.append(rt_rating)
        logging.debug(line)
        for media_type in media_types:
            if media_type in movie['media_types']:
                type_strings = []
                for provider,details in movie['media_types'][media_type].items():
                    name = provider
                    if details['price'] > 0:
                        name = "%s - $%s" % (provider, details['price'])
                    type_strings.append(
                        ("[%s](%s)" % ( name, details['url'] )).replace(' ','&nbsp;')
                    )
                type_joined = ' &#183; '.join(type_strings)
            else:
                type_joined = ''
            line.append(type_joined)
        ret_line.append('|'.join(line))
    # If we don't have streams for any movies, we shouldn't comment
    # Only return the formatted text if we have useful info
    if actual_links:
        return "\n".join(ret_line)
    else:
        return False

def ignore_message(message):
    response = None
    author  = message['author']
    body    = message['body']
    date    = datetime.datetime.fromtimestamp(int(message['created_utc']))
    subject = message['subject'].lower()
    message_id = int(message['id'],36)
    # If subject == IGNORE ME
    if subject == "ignore me":
        if is_author_ignored(author):
            # We're already ignoring this user.
            logging.info("Request to ignore %s when already ignoring this user. Skipping" % author)
        else:
            # Add username to DB to be ignored
            logging.info("Adding %s to the ignore list" % author)
        ignored = True
        response =  (
            "Sorry to hear you want me to ignore you. Was it something "
            "I said? I will not reply to any posts you make in the future. "
            "If you want me to reply to your posts, you can send me "
            "[a message](%s). Also, if you "
            "wouldn't mind filling out [this survey](%s) "
            "giving me feedback, I'd really appreciate it. It would make me a better bot" %
            (REDDIT_PM_REMEMBER,REDDIT_PM_FEEDBACK)
        )
    # If subject ==  REMEMBER ME
    elif subject == "remember me":
        # Remove username from DB
        logging.info("No longer ignoring %s" % author)
        ignored = False
        response = (
            "Ok, I'll reply to your posts from now on. "
            "If you want me to stop, you can send me "
            "[a message](%s), "
            "and I'll stop replying to your posts" %
            (REDDIT_PM_IGNORE)
        )
    ignore_key = author_ignore_key(author)
    if not ignore_key:
        ignore_key = IgnoreList()
    ignore_key.message_id = message_id
    ignore_key.message_date = date
    ignore_key.body = body
    ignore_key.author = author
    ignore_key.ignored = ignored
    ignore_key.put()
    return response

def add_to_list(message):
    author  = message['author']
    body    = message['body']
    date    = datetime.datetime.fromtimestamp(int(message['created_utc']))
    subject = message['subject'].lower()
    message_id = int(message['id'],36)
    # Get the subreddit in the message
    match = re.search(r'r/(\w+)',body)
    if not match:
        return False
    subreddit = match.group(1)
    logging.info("Subreddit is %s" % subreddit)
    # Check to see if user is moderator of subreddit
    # If not, abort now
    if reddit.is_user_moderator(subreddit,author):
        # Else, see what they want to do
        if subject == "whitelist":
            list_type = 'white'
            remove_from_entity = Blacklisted
            entity = Whitelisted
        elif subject == "blacklist":
            list_type = 'black'
            remove_from_entity = Whitelisted
            entity = Blacklisted
        if not is_listed(list_type,subreddit):
            # Subreddit is not listed
            entity(
                subreddit = subreddit,
                updated_by = author
            ).put()
            old_entity = remove_from_entity.query(remove_from_entity.subreddit==subreddit).get()
            if old_entity:
                # Delete the entity from the 
                # other list. We can't have a subreddit on both lists
                old_entity.key.delete()
            logging.info("%s is now %slisted because of %s" % (subreddit,list_type,author))
            subreddit_mods = "/r/%s" %subreddit
            reply_subject = "%s added to /u/%s %s" % (subreddit_mods,config.reddit['user'],subject)
            response = (
                "This message is to inform you that the request by %s "
                "to %s /r/%s has been processed. /u/%s will respect "
                "this decision moving forward. You can find out more "
                "about what this means by referring to [this wiki](%s)"
                % (author,subject,subreddit,config.reddit['user'],REDDIT_PM_MODS)
            )
            reddit.send_message(subreddit_mods,reply_subject,response)
    else:
        logging.warning("%s is not a moderator of %s. Abort" % (author,subreddit))
    return None

def delete_message(message):
    response = None
    author = message['author']
    body = message['body']
    body_regex = re.search(r'delete (?P<thing_name>(?P<thing_type>t\d)_(?P<thing_id>\w+))',body)
    if not body_regex:
        logging.info("Couldn't find a anything to delete in the message")
        return None
    thing_name = str(body_regex.group('thing_name'))
    thing_type = body_regex.group('thing_type')
    logging.debug("thing_name: %s; thing_type: %s" % (thing_name,thing_type))
    # Figure out what the thing they want us to delete is
    if thing_type != "t1":
        logging.info("Received Delete request for unknown thing type %s" % thing_type)
        return None
    # This thing is a comment
    # Lookup this thing in the DB
    logging.debug("Searching for a post with a comment of %s" % thing_name)
    comments = Comment.query(
        Comment.name == thing_name,
    ).fetch()
    logging.debug(comments)
    for comment in comments:
        logging.debug(comment)
        post = comment.key.parent().get()
        original_author = post.author
        # If the author is the same as the author in question
        if original_author == author:
            logging.info("Message from %s matches OP %s. Will delete %s" %(author,original_author,thing_name))
            # Delete post
            reddit.delete_from_reddit(thing_name)
            comment.deleted = True
            comment.put()
            response =  textwrap.dedent("""
                Ok, I deleted my comment on your post. Sorry about that.
                If you never want me to respond to you again, I understand. you can always send
                [a message](%s), and I'll never ever respond to your post,
                I promise. Also, if you wouldn't mind filling out
                [this survey](%s) giving me feedback, I'd really appreciate
                it. It would make me a better bot.
                """ % (REDDIT_PM_IGNORE,REDDIT_PM_FEEDBACK))
        else:
            # Delete request isn't from OP. Don't delete
            logging.info("%s isn't the OP. Will not delete %s" % (author,thing_name))
    return response

reddit = Reddit()
mh = MediaHound()

def search_process_reddit_posts(query,summoned=False,recursive=True,after=None):
    if after is not None:
        new_query = "%s&after=%s" % (query,after)
    else:
        new_query = query
    logging.debug("Searching Reddit with the following query: %s. Summoned is %s" % (new_query,summoned))
    search_results = reddit.search_reddit(new_query)
    if search_results:
        for post in search_results['data']['children']:
            logging.debug(post)
            post_id = post['data']['name']
            if ndb.Key(Post, post_id).get():
                recursive = False
            taskqueue.add(
                url='/tasks/process_post',
                queue_name='processPost',
                params={
                    'post': post_id,
                    'summoned':summoned,
                    'post_data':json.dumps(post),
                }
            )
        next_after = search_results['data']['after']
        if recursive and next_after is not None:
            search_process_reddit_posts(
                query,
                summoned,
                after=next_after
            )

# Performs a search for posts with imdb links in the title,
# selftext, and url. For each post, send to comment on post
class search_imdb(webapp2.RequestHandler):
    def get(self):
        search_process_reddit_posts("title%3Aimdb.com+OR+url%3Aimdb.com+OR+imdb.com")

class search_usermention(webapp2.RequestHandler):
    def get(self):
        search_process_reddit_posts(
            "title%3A/u/{u}+OR+url%3A/u/{u}+OR+/u/{u}".format(u=config.reddit['user']),
            summoned=True
        )

class manual_process(webapp2.RequestHandler):
    def get(self,post_id):
        logging.info("Forcing processing on post %s" % post_id)
        # Add the task to the default queue.
        taskqueue.add(
            url='/tasks/process_post',
            queue_name='processPost',
            params={
                'post': post_id,
                'forced':True
            }
        )

class process_post(webapp2.RequestHandler):
    def post(self):
        post_id   = self.request.get('post')
        forced    = True if self.request.get('forced')   == 'True' else False
        summoned  = True if self.request.get('summoned') == 'True' else False
        post_data = self.request.get('post_data')
        if post_data:
            post_data = json.loads(post_data)
        # Check that the post id is formatted properly
        logging.info("Begin processing post with name: %s. Forced is %s and summoned is %s" % (post_id,forced,summoned))
        logging.debug(post_data)
        post = PostObject(post_id,post_data)
        if post.processing is False:
            lookup_movie_data(post.movies_list)
            if post.commented is False or forced is True:
                if should_comment(post=post,forced=forced,summoned=summoned):
                    comment_on_post(post,summoned)
                else:
                    logging.info("Determined I shouldn't comment on this post for one reason or another")
            else:
                logging.info("I've already commented on this post. Not commenting this time")
        else:
            logging.info("This post is already being processed")

# Reads unread messages from the inbox. 
class read_messages(webapp2.RequestHandler):
    def get(self):
        logging.info("Getting list of unread messages")
        # Get unread messages
        unread = reddit.get_unread_messages()
        if unread:
            logging.debug("Received the following response for unread messages %s" % unread)
            for message in unread['data']['children']:
                response = None
                author = message['data']['author']
                name = message['data']['name']
                if message['data']['was_comment']:
                    if 'subject' in message['data']:
                        subject = message['data']['subject']
                        if subject == 'username mention':
                            post_id = message['data']['name']
                            logging.info("Got username mention")
                            taskqueue.add(
                                url='/tasks/process_post',
                                queue_name='processPost',
                                params={
                                    'post'     : post_id,
                                    'summoned' : True,
                                    'post_data': json.dumps(message)
                                }
                            )
                        elif subject == 'comment reply':
                            logging.info("Got a comment reply. I don't know how to handle this. I need a human")
                        else:
                            logging.info("Got a comment with subject %s. I need a human." % subject)
                else:
                    subject = message['data']['subject'].lower()
                    logging.info("Got a message from %s with the subject %s" % (author,subject))
                    if subject in ["ignore me", "remember me"]:
                        response = ignore_message(message['data'])
                    elif subject in ["blacklist","whitelist"]:
                        response = add_to_list(message['data'])
                    elif subject == "delete":
                        response = delete_message(message['data'])
                    elif subject == "process" or subject == "re: process":
                        response = pm_summon(message['data'])
                    else:
                        logging.info("Got a random message. I don't know how to handle this. I need a human.")
                # Mark message as read
                if reddit.mark_message_read(name):
                    if response is not None:
                        # Reply to the user
                        reply_subject = "re: %s" %subject
                        logging.info("Replying with subject: %s and response %s" % (reply_subject,response))
                        reddit.post_to_reddit(name,response)
        else:
            logging.error("Error getting unread messages")

class review_comment(webapp2.RequestHandler):
    def post(self):
        comment_id = self.request.get('comment_id')
        post_id    = self.request.get('post_id')
        logging.info("Need to do a checkup on comment %s" % comment_id)
        comment_key = ndb.Key(Post, post_id, Comment, comment_id)
        logging.debug(comment_key)
        comment = comment_key.get()
        if not comment:
            logging.error("Couldn't find comment %s in the DB" % comment_id)
            return None
        comment_revision_num = comment.revision
        revision_key = ndb.Key(Post, post_id, Comment, comment_id, CommentRevisions, str(comment_revision_num))
        post_results = reddit.api_call("https://oauth.reddit.com/api/info.json?id=%s" % comment_id)
        if post_results:
            if post_results['data']['children']:
                comment_data = post_results['data']['children'][0]
                logging.debug("Got back the following data for comment: %s. Data: %s" % (comment_id,comment_data))
                score = comment_data['data']['score']
                comment.score = score
                logging.info("Comment %s has a score of %d" % (comment_id,score))
                if score < -2: # TODO: Remove this hardcoded threshold
                    logging.info("Deleting comment %s because of a low score" % comment_id)
                    # This score is less than what we want. Delete the post
                    reddit.delete_from_reddit(comment_id)
                    comment.deleted = True
                    logging.info("Comment %s is deleted" % comment_id)
                else:
                    comment_revision = revision_key.get()
                    if not comment_revision:
                        logging.error("Couldn't find revision %d for comment %s" % (comment_revision_num,comment_id))
                        return None
                    logging.info("Need to check if we should recheck the contents of this post")
                    post = PostObject(post_id)
                    orig_text = comment_revision.body
                    if not post.movies_list:
                        logging.info("No movies in parent post")
                        return None
                    updated_text = format_new_post(get_movie_data(post.movies_list))
                    if updated_text is not False and len(updated_text) > len(orig_text):
                        logging.info("The updated text is more than what we originally commented on. Perhaps we should edit the comment")
                        # Edit the comment, and update the revision in the DB
                        update_comment(post_id,comment_id,updated_text)
                        logging.debug("New comment text is %s. Old text was %s" % (updated_text,orig_text))
                    else:
                        logging.info("No need to edit the comment since updated text is not longer than what we have")
                comment.put()
            else:
                logging.info("No children returned when searching for comment: %s" % comment_id)
        else:
            logging.error("Unable to get results for comment %s" % comment_id)
            # Throw error to get out of here

class check_comments(webapp2.RequestHandler):
    def get(self):
        date_search = datetime.datetime.now() - datetime.timedelta(days=7)
        comments = Comment.query(ndb.AND(
            Comment.post_date > date_search,
            Comment.deleted == False,
        )).fetch()
        for comment in comments:
            logging.debug(comment)
            post = comment.key.parent().get()
            logging.debug("The key for this comment is %s and parent is %s" % (comment.key,post.name))
            taskqueue.add(
                url='/tasks/review_comment',
                queue_name='reviewComment',
                params={
                    'comment_id' : comment.name,
                    'post_id'    : post.name,
                }
            )

class update_wiki_lists(webapp2.RequestHandler):
    def get(self):
        subreddit = config.subreddit
        lists = {'white':Whitelisted,'black':Blacklisted}
        for list_type in lists:
            entity = lists[list_type]
            listed_subreddits = []
            for item in entity.query().fetch():
                listed_subreddits.append("/r/%s/" % item.subreddit)
            content = '\n\n'.join(listed_subreddits)
            page = "%slisted" % list_type
            reason = "Automated update of %slisted subreddits" % list_type
            if reddit.update_wiki(subreddit,page,content,reason):
                logging.info("Sucessfully updated the %slisted wiki in /r/%s" % (list_type,subreddit))
            else:
                logging.error("Error updating the %slisted wiki in /r/%s" % (list_type,subreddit))

class delete_all_posts(webapp2.RequestHandler):
    def get(self):
        ndb.delete_multi(
            Post.query().fetch(keys_only=True)
        )
        
application = webapp2.WSGIApplication([
    ('/tasks/search/imdb', search_imdb),
    ('/tasks/search/user', search_usermention),
    ('/tasks/manual/(\w+)', manual_process),
    ('/tasks/review_comment', review_comment),
    ('/tasks/process_post', process_post),
    ('/tasks/delete_all_posts', delete_all_posts),
    ('/tasks/inbox', read_messages),
    ('/tasks/check_comments',check_comments),
    ('/tasks/wiki', update_wiki_lists)
],
    debug=True
)
