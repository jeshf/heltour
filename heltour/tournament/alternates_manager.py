from heltour.tournament.models import *
from django.core.urlresolvers import reverse
import reversion

def current_round(season):
    if not season.alternates_manager_enabled():
        return None
    return season.round_set.filter(publish_pairings=True, is_completed=False).order_by('number').first()

def do_alternate_search(season, board_number):
    round_ = current_round(season)
    if round_ is None:
        return
    setting = season.alternates_manager_setting()
    print 'Alternate search on bd %d' % board_number

    # Figure out which players need to be replaced and which alternates have/haven't been contacted
    player_availabilities = PlayerAvailability.objects.filter(round=round_, is_available=False) \
                                                      .select_related('player').nocache()
    availability_modified_dates = {pa.player: pa.date_modified for pa in player_availabilities}
    round_pairings = TeamPlayerPairing.objects.filter(team_pairing__round=round_) \
                                              .select_related('white', 'black').nocache()
    players_in_round = {p.white for p in round_pairings} | {p.black for p in round_pairings}
    board_pairings = TeamPlayerPairing.objects.filter(team_pairing__round=round_, board_number=board_number, result='', game_link='') \
                                              .select_related('white', 'black').nocache()
    players_on_board = {p.white for p in board_pairings} | {p.black for p in board_pairings}
    teams_by_player = {p.white: p.white_team() for p in board_pairings}
    teams_by_player.update({p.black: p.black_team() for p in board_pairings})

    unavailable_players = {pa.player for pa in player_availabilities}
    # Prioritize open spots by the date the player was marked as unavailable
    players_that_need_replacements = sorted(players_on_board & unavailable_players, key=lambda p: availability_modified_dates[p])
    alternates_contacted = Alternate.objects.filter(season_player__season=season, board_number=board_number, status='contacted')
    last_declined_alternate = Alternate.objects.filter(season_player__season=season, board_number=board_number, status='declined') \
                                               .order_by('-last_contact_date').first()
    alternates_not_contacted = sorted(Alternate.objects.filter(season_player__season=season, board_number=board_number, status='waiting') \
                                                       .select_related('season_player__registration', 'season_player__player').nocache(), \
                                      key=lambda a: a.priority_date())

    if len(players_that_need_replacements) == 0:
        # No searches in progress, so notify and update the status of previously-contacted alternates
        for alt in alternates_contacted:
            if alt.last_contact_date is not None and timezone.now() - alt.last_contact_date > setting.unresponsive_interval:
                alt.status = 'unresponsive'
            else:
                alt.status = 'waiting'
            with reversion.create_revision():
                reversion.set_comment('Alternate spots filled')
                alt.save()
            signals.alternate_spots_filled.send(sender=do_alternate_search, alternate=alt, response_time=setting.unresponsive_interval)
        return

    # Continue the search for an alternate to fill each open spot
    for p in players_that_need_replacements:
        search, created = AlternateSearch.objects.get_or_create(round=round_, team=teams_by_player[p], board_number=board_number)
        if not search.is_active or search.status == 'all_contacted':
            # Search is over (or was manually disabled), move on to the next open spot
            continue

        if created or search.status == 'completed':
            # Search has just (re)started
            signals.alternate_search_started.send(sender=do_alternate_search, season=season, team=teams_by_player[p], \
                                                  board_number=board_number, round_=round_)
            with reversion.create_revision():
                reversion.set_comment('Alternate search started')
                search.status = 'started'
                search.save()

        # Figure out if it's time to contact the next alternate on the list
        # If not, we don't need to do anything for this open spot until the next tick
        if search.last_alternate_contact_date is None:
            # The search has just started
            do_contact = True
        elif last_declined_alternate is not None and search.last_alternate_contact_date == last_declined_alternate.last_contact_date:
            # The most-recently-contacted alternate declined
            do_contact = True
        else:
            # Check if it has been long enough since the last alternate was contacted
            time_since_last_contact = timezone.now() - search.last_alternate_contact_date
            contact_interval = setting.contact_interval if round_.publish_pairings else setting.contact_interval_before_round_start
            do_contact = time_since_last_contact >= contact_interval

        if do_contact:
            try:
                # Figure out which alternate to contact
                while True:
                    # This will throw an IndexError if no alternates are left on the list
                    alt_to_contact = alternates_not_contacted.pop(0)
                    # If the alternate is unavailable, or already playing a game this round, or has a red card,
                    # continue the loop and try the next one
                    if alt_to_contact.season_player.player not in unavailable_players and \
                            alt_to_contact.season_player.player not in players_in_round and \
                            alt_to_contact.season_player.games_missed < 2:
                        break

                # Contact the alternate, providing them with a pair of private links to respond
                alt_username = alt_to_contact.season_player.player.lichess_username
                league_tag = season.league.tag
                season_tag = season.tag
                auth = PrivateUrlAuth.objects.create(authenticated_user=alt_username, expires=round_.end_date)
                accept_url = reverse('by_league:by_season:alternate_accept_with_token', args=[league_tag, season_tag, auth.secret_token])
                decline_url = reverse('by_league:by_season:alternate_decline_with_token', args=[league_tag, season_tag, auth.secret_token])
                signals.alternate_needed.send(sender=do_alternate_search, alternate=alt_to_contact, response_time=setting.unresponsive_interval, \
                                              accept_url=accept_url, decline_url=decline_url)
                current_date = timezone.now()
                with reversion.create_revision():
                    reversion.set_comment('Alternate contacted')
                    alt_to_contact.status = 'contacted'
                    alt_to_contact.last_contact_date = current_date
                    alt_to_contact.save()
                with reversion.create_revision():
                    reversion.set_comment('Alternate contacted')
                    search.last_alternate_contact_date = current_date
                    search.save()
            except IndexError:
                # No alternates left, so the search is over
                # The spot can still be filled if previously-contacted alternates end up responding
                signals.alternate_search_all_contacted.send(sender=do_alternate_search, season=season, team=teams_by_player[p], \
                                            board_number=board_number, round_=round_, number_contacted=len(alternates_contacted))
                with reversion.create_revision():
                    reversion.set_comment('All alternates contacted')
                    search.status = 'all_contacted'
                    search.save()

def alternate_accepted(alternate):
    # This is called by the alternate_accept endpoint
    # The alternate gets there via a private link sent to their slack
    season = alternate.season_player.season
    round_ = current_round(season)
    # Validate that the alternate is in the correct state
    if alternate.status != 'contacted':
        return False
    # Validate that the alternate doesn't already have a game in the round
    # Players can sometimes play multiple games (e.g. playing up a board), but that isn't done through the alternates manager
    if (TeamPlayerPairing.objects.filter(team_pairing__round=round_, white=alternate.season_player.player) | \
        TeamPlayerPairing.objects.filter(team_pairing__round=round_, black=alternate.season_player.player)).nocache().exists():
        return False
    # Find an open spot to fill, prioritized by the time the search started
    active_searches = AlternateSearch.objects.filter(round=round_, board_number=alternate.board_number, is_active=True) \
                                             .order_by('date_created').select_related('team').nocache()
    for search in active_searches:
        if search.still_needs_alternate():
            with reversion.create_revision():
                reversion.set_comment('Alternate assigned')
                assignment, _ = AlternateAssignment.objects.update_or_create(round=round_, team=search.team, board_number=search.board_number, \
                                                                         defaults={'player': alternate.season_player.player, 'replaced_player': None})
            with reversion.create_revision():
                reversion.set_comment('Alternate assigned')
                alternate.status = 'accepted'
                alternate.save()
            with reversion.create_revision():
                reversion.set_comment('Alternate search completed')
                search.status = 'completed'
                search.save()
            signals.alternate_assigned.send(sender=alternate_accepted, season=season, alt_assignment=assignment)
            return True
    return False

def alternate_declined(alternate):
    # This is called by the alternate_decline endpoint
    # The alternate gets there via a private link sent to their slack
    if alternate.status == 'waiting' or alternate.status == 'contacted':
        with reversion.create_revision():
            reversion.set_comment('Alternate declined')
            alternate.status = 'declined'
            alternate.save()
