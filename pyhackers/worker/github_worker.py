import logging
from datetime import datetime as dt
from dateutil import parser as dt_parser
#from cqlengine import BatchQuery
from cqlengine.query import DoesNotExist
from pyhackers.common import unix_time
from pyhackers.config import config
from pyhackers.model.user import User, SocialUser
from pyhackers.model.cassandra.hierachy import (GithubProject,
                                                GithubUserList,
                                                GithubUser,
                                                GithubEvent,
                                                )
from github import Github
import requests
import simplejson

GITHUB_URL = "https://api.github.com/{}?client_id={}&client_secret={}"


class RegistrationGithubWorker():
    """
    Once a user registers via GitHub, we will fetch the stars/watching projects
    following users/followers, events that the user will see their event stream.
    """

    def __init__(self, user_id, social_account_id, config):
        self.user_id = user_id
        self.social_account_id = social_account_id
        self.client_id = config.get("github", 'client_id')
        self.client_secret = config.get("github", 'client_secret')
        self.access_token = None
        self.g = None
        self.github_user = None
        self.github_user_detail = None
        self.users_discovered = set()
        self.previous_link = None

    def get_user_details_from_db(self):
        user = User.query.get(self.user_id)
        social_account = SocialUser.query.get(self.social_account_id)
        self.access_token = social_account.access_token

    def init_github(self):
        self.g = Github(self.access_token,
                        client_id=self.client_id,
                        client_secret=self.client_secret,
                        per_page=100)

        self.github_user = self.g.get_user()
        self.github_user_detail = GithubUserList.create(user=self.github_user.login)

    def get_starred_projects(self):
        starred = self.github_user.get_starred()
        projects = []
        #with BatchQuery() as b:
        for s in starred:
            projects.append(s.full_name)
            self.users_discovered.add(s.owner.login)

            GithubProject.create(
                id=s.id,
                name=s.name,
                full_name=s.full_name,
                watchers_count=s.watchers,
                description=s.description,
                homepage=s.homepage,
                fork=s.fork,
                forks_count=s.forks,
                language=s.language,
                master_branch=s.master_branch,
                network_count=0,
                open_issues=s.open_issues,
                url=s.url,
                is_py=s.language in ['python', 'Python'],
                owner=s.owner.id,
                hide=False,
            )

            #print s.full_name, s.watchers

        self.github_user_detail.starred = projects
        self.github_user_detail.save()

    def get_following_users(self):
        following = self.github_user.get_following()
        following_users = []

        for f in following:
            self.users_discovered.add(f.login)
            following_users.append(f.login)
            print f

        self.github_user_detail.following = following_users
        self.github_user_detail.save()

    def get_follower_users(self):
        followers = self.github_user.get_followers()
        follower_users = []

        for f in followers:
            self.users_discovered.add(f.login)
            follower_users.append(f.login)

        self.github_user_detail.followers = follower_users
        self.github_user_detail.save()

    def save_discovered_users(self):

        found_ids = GithubUser.objects.filter(nick__in=list(self.users_discovered))
        found_id_list = []

        for user in found_ids:
            found_id_list.append(user.nick)

        missing_ids = list(set(self.users_discovered) - set(found_id_list))

        logging.warn(found_id_list)
        logging.warn(self.users_discovered)

        logging.warn(u"[{}] users are found".format(len(self.users_discovered)))
        logging.warn(u"[{}] users are missing".format(len(missing_ids)))

        #return

        for nick in missing_ids:
            user = self.g.get_user(nick)

            logging.warn(u"Creating user [{}]".format(nick))

            GithubUser(nick=user.login,
                       id=user.id,
                       email=user.email,
                       followers=user.followers,
                       following=user.following,
                       image=user.avatar_url,
                       blog=user.blog,
                       bio=user.bio,
                       company=user.company,
                       location=user.location,
                       name=user.name,
                       url=user.url,
                       utype=user.type,
                       public_repos=user.public_repos,
                       public_gists=user.public_gists, ).save()

            logging.warn(u"User[{}]created".format(nick))

    @staticmethod
    def _create_event(json_resp):
        for event in json_resp:
            event_id = int(event.get("id", None))
            event_type = event.get("type", "").replace("Event", "")
            actor = event.get("actor", None)
            actor_str = "{},{}".format(actor.get("id", ""), actor.get("login"))
            repo = event.get("repo", None)
            repo_str = None

            if repo is not None:
                repo_str = "{},{}".format(repo.get("id", ""), repo.get("name"))

            public = event.get("public", None)

            created_at = unix_time(dt_parser.parse(
                event.get("created_at", dt.utcnow())).replace(tzinfo=None),
                float=False)
            org = event.get("org", None)
            org_str = None
            if org is not None:
                org_str = "{},{}".format(org.get("id", ""), org.get("login"))

            payload = event.get("payload", {})

            GithubEvent.create(id=event_id, type=event_type,
                               actor=actor_str, org=org_str,
                               repo=repo_str, created_at=created_at, payload=simplejson.dumps(payload))

    def get_user_timeline(self):
        """
        Fetch events from github
        """
        user = self.github_user.login

        url = GITHUB_URL.format("users/{}/received_events/public".format(user),
                                self.client_id, self.client_secret)
        session = requests.session()

        def paged_json_request(endpoint):
            """
            Recursively fetch the events
            response headers contain ['link'] which is the next or previous page url.
            """
            r = session.get(endpoint)

            json_resp = r.json()
            logging.debug(r.headers)
            next_link = r.headers.get("link")

            logging.debug(next_link)

            if next_link is not None and len(next_link) > 0:
                try:

                    link_parts = next_link.split(";")
                    next_url = link_parts[0]
                    direction = link_parts[1]

                    if "prev" in direction:
                        return

                    next_request_url = next_url.replace("<", "").replace(">", "")

                    if self.previous_link == next_request_url:
                        return

                    self.previous_link = next_request_url

                except Exception, ex:
                    logging.error(ex)
                    next_request_url = None
            else:
                logging.warn(40 * "=")
                logging.warn("No more next link")
                return

            self._create_event(json_resp)

            paged_json_request(next_request_url)

        paged_json_request(url)

    def run(self):
        self.get_user_details_from_db()
        self.init_github()
        self.get_starred_projects()
        self.get_following_users()
        self.get_follower_users()
        self.save_discovered_users()
        self.get_user_timeline()
        # TODO: Generate User Stories


def new_github_registration(user_id, social_account_id):
    logging.warn("[TASK][new_github_registration]: [UserId:{}] [SAcc:{}]".format(user_id, social_account_id))

    RegistrationGithubWorker(user_id, social_account_id, config).run()


if __name__ == "__main__":
    #new_github_registration(12,5)
    new_github_registration(14, 13)