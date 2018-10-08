#!/bin/env python

# Accounting file parser, to answer questions such as:
# - What is the breakdown of usage between faculties?
# - What is the breakdown of usage between users?
# - What is the breakdown of usage between users within a faculty?

# Try and be python2 compatible
from __future__ import print_function

import argparse
import os
import re
import sys
import math
import sge
import datetime
import time
import pytz

from tabulate import tabulate
from functools import reduce
from dateutil.relativedelta import relativedelta

# Command line arguments
# ----------------------

parser = argparse.ArgumentParser(description='Report on accounting data')
parser.add_argument('--date', action='store', type=str, help="Date range in UTC to report on, format [DATE][-[DATE]] where DATE has format YYYY[MM[DD[HH[MM[SS]]]]] e.g. 2018 for that year, 2018-2019 for two years, -2018 for everything up to the start of 2018, 2018- for everything after the start of 2018, 201803 for March 2018, 201806-201905 for 12 months starting June 2018")
parser.add_argument('--skipqueues', action='append', type=str, help="Queue to filter out")
parser.add_argument('--queues', action='append', type=str, help="Queue to report on")
parser.add_argument('--owners', action='append', type=str, help="Equipment owner to report on")
parser.add_argument('--skipowners', action='append', type=str, help="Equipment owner to filter out")
parser.add_argument('--coreowners', action='store_true', default=False, help="Report on core set of equipment owners")
parser.add_argument('--limitusers', action='store', type=int, default=sys.maxsize, help="Report on n most significant users")
parser.add_argument('--accountingfile', action='append', type=str, help="Read accounting data from file")
parser.add_argument('--cores', action='store', default=0, type=int, help="Total number of cores to report utilisation on")
parser.add_argument('--reports', action='append', type=str, help="What information to report on (default: header, owners, users, usersbyowner)")

args = parser.parse_args()

# Prepare regexes
# ---------------

time_startend_def = re.compile(r"^(\d+)?(-(\d+)?)?$")

datetime_def = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?$")

owner_def = re.compile(r"^([a-z]+_)?(\S+)")

# Init parameters
# ---------------

# Maximum date to report on (YYYY[MM[DD[HH[MM[SS]]]]])
max_date = "40000101"

# Backup method of determining node memory per core (mpc), in absence of
# node_type in job record, from hostname
backup_node_mpc = [
   { 'regex': r"^h7s3b1[56]", 'mpc': sge.number("64G") // 24 }, # ARC2
   { 'regex': r"^h[12367]s",  'mpc': sge.number("24G") // 12 }, # ARC2
   { 'regex': r"^dc[1-4]s",   'mpc': sge.number("128G") // 24 }, # ARC3
   { 'regex': r"^c2s0b[0-3]n",'mpc': sge.number("24G") // 8 }, # ARC1
   { 'regex': r"^c[1-3]s",    'mpc': sge.number("12G") // 8 }, # ARC1
   { 'regex': r"^smp[1-4]",   'mpc': sge.number("128G") // 16 }, # ARC1
   { 'regex': r"^g8s([789]|10)n", 'mpc': sge.number("256G") // 16 }, # POLARIS
   { 'regex': r"^g[0-9]s",    'mpc': sge.number("64G") // 16 }, # POLARIS/ARC2
   { 'regex': r"^hb01s",      'mpc': sge.number("256G") // 20 }, # MARC1
   { 'regex': r"^hb02n",      'mpc': sge.number("3T") // 48 }, # MARC1
]

# Compile regexes
for n in backup_node_mpc:
   n['re'] = re.compile(n['regex'])

# ARC1 accounting file needs a different method to distinguish
# equipment owners beyond the core owners
queue_owner_mapping = {
   'env1_sgpc.q': 'sgpc',
   'env1_glomap.q': 'glomap',
   'speme1.q': 'speme',
   'env1_neiss.q': 'neiss',
   'env1_tomcat.q': 'tomcat',
   'chem1.q': 'chem',
   'civ1.q': 'civil',
   'mhd1.q': 'mhd',
}

# Parent of owner mapping (e.g. mapping to core purchasers)
owner_parent_mapping = {
   'ENV': 'ENV',
   'ENG': 'ENG',
   'MAPS': 'MAPS',
   'FBS': 'FBS',
   'ARC': 'ARC',
   'Arts': 'Arts',
   'LUBS': 'LUBS',
   'ESSL': 'ESSL',
   'PVAC': 'PVAC',
   'MEDH': 'MEDH',

   'minphys': 'ENV',
   'glocat': 'ENV',
   'glomap': 'ENV',
   'tomcat': 'ENV',
   'palaeo1': 'ENV',
   'sgpc': 'ENV',
   'neiss': 'ENV',

   'speme': 'ENG',
   'civil': 'ENG',

   'mhd': 'MAPS',
   'skyblue': 'MAPS',
   'chem': 'MAPS',
   'maths': 'MAPS',
   'astro': 'MAPS',
   'codita': 'MAPS',

   'omics': 'FBS',
   'cryoem': 'FBS',
}

# Some owners have changed names, or combined with other
# owners over the years.
owner_owner_mapping = {
   'ISS': 'ARC',
   'UKMHD': 'MAPS',
}

# Routines
# --------

def main():
   # Restrict to the core purchasers of ARC, if requested
   if args.coreowners:
      args.owners = [ 'Arts', 'ENG', 'ENV', 'ESSL', 'FBS', 'LUBS', 'MAPS', 'MEDH', 'PVAC' ]

   # Read default accounting file if none specified
   if not args.accountingfile:
      args.accountingfile = [ os.environ["SGE_ROOT"] + "/" + os.environ["SGE_CELL"] + "/common/accounting" ]

   # All reports, if not specified
   if not args.reports:
      args.reports = [ 'header', 'totals', 'owners', 'users', 'usersbyowner' ]

   # Parse date argument
   global start_time, end_time
   start_time, end_time = parse_startend(args.date)

   # Collect raw data, split by equipment owner
   owners = {}
   for accounting in args.accountingfile:
      for record in sge.records(accounting=accounting, modify=record_modify, filter=record_filter):
         user = record['owner'] # (refers to job owner here, not equipment owner)
         owner = record['equip_owner'] # (refers to the equipment owner)

         # - init data
         if owner not in owners:
            owners[owner] = {}

         if user not in owners[owner]:
            owners[owner][user] = { 'jobs': 0, 'time': 0, 'time_adj': 0 }

         # - record usage
         owners[owner][user]['jobs'] += 1

         t = core_hours(record)
         owners[owner][user]['time'] += t[0]
         owners[owner][user]['time_adj'] += t[1]

   # Calculate a summary for each user
   users = {}
   for owner in owners:
      for user in owners[owner]:
         if user not in users:
            users[user] = { 'jobs': 0, 'time': 0, 'time_adj': 0 }

         users[user]['jobs'] += owners[owner][user]['jobs']
         users[user]['time'] += owners[owner][user]['time']
         users[user]['time_adj'] += owners[owner][user]['time_adj']

   # Calculate a summary for each owner
   owner_summaries = {}
   for owner, data in owners.items():
      owner_summaries[owner] = { 'users': 0, 'jobs': 0, 'time': 0, 'time_adj': 0 }

      for user in data.values():
         owner_summaries[owner]['users'] += 1
         owner_summaries[owner]['jobs'] += user['jobs']
         owner_summaries[owner]['time'] += user['time']
         owner_summaries[owner]['time_adj'] += user['time_adj']

   # Spit out answer
   print_summary(owners, users, owner_summaries, args.cores, args.reports)


def record_filter(record):
   # - Time filtering
   if record['end_time'] < start_time or record['start_time'] >= end_time: return False

   # - Queue filtering
   if args.skipqueues and record['qname'] in args.skipqueues: return False
   if args.queues and record['qname'] not in args.queues: return False

   # - Owner filtering
   if args.skipowners and record['equip_owner'] in args.skipowners: return False
   if args.owners and record['equip_owner'] not in args.owners: return False

   return True


def record_modify(record):
   # - Add record equipment owner in record
   r = owner_def.match(record['project'])
   if r:
      owner = r.group(2)

      # - queue to owner mapping
      if record['qname'] in queue_owner_mapping:
         owner = queue_owner_mapping[record['qname']]

      # - owner to owner mapping (name changes, mergers, etc.)
      if owner in owner_owner_mapping:
         owner = owner_owner_mapping[owner]
   else:
      owner = '<unknown>'

   record['equip_owner'] = owner


def print_summary(owners, users, owner_summaries, total_cores, reports):

   if 'header' in reports:
      print("Accounting summary, reporting on jobs ending in the range:")
      print(" Start:", time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime(start_time)))
      print(" End:", time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime(end_time)))
      print(" Duration:", (end_time - start_time)//3600, "hours", "Cores:", total_cores)
      print("")

   # Note: lots of promoting ints to floats for two reasons:
   # - make divisions behave the same on python2
   # - tabulate only provides the option to separate thousands for floats

   ##DEBUG - figure out what to do here when range is for all time
   if total_cores:
      inv_total_time = 1/float((end_time - start_time)* total_cores)
   else:
      inv_total_time = 0

   time_adj = reduce((lambda x, k: x + owner_summaries[k]['time_adj']), owner_summaries, 0)

   if 'totals' in reports:
      print("=======")
      print("Totals:")
      print("=======\n")

      h = [ 'Owners', 'Uniq Usrs', 'Jobs', 'Core Hrs', '%Utl', 'Adj Core Hrs', '%Utl' ]
      d = [
         [
            float(len(owner_summaries)),
            float(len(users)),
            float(reduce((lambda x, k: x + owner_summaries[k]['jobs']), owner_summaries, 0)),
            float(reduce((lambda x, k: x + owner_summaries[k]['time']), owner_summaries, 0) / float(3600)),
            percent(reduce((lambda x, k: x + owner_summaries[k]['time'] * inv_total_time), owner_summaries, 0)),
            float(reduce((lambda x, k: x + owner_summaries[k]['time_adj']), owner_summaries, 0) / float(3600)),
            percent(reduce((lambda x, k: x + owner_summaries[k]['time_adj'] * inv_total_time), owner_summaries, 0)),
         ],
      ]

      print(tabulate(d, headers=h, floatfmt=",.0f"),"\n")

   if 'owners' in reports:
      h = [ 'Owner', 'Parent', 'Uniq Usrs', 'Jobs', 'Core Hrs', '%Utl', 'Adj Core Hrs', '%Utl', '%Usg' ]

      print("===========")
      print("Top owners:")
      print("===========\n")

      d = []
      for owner, data in sorted(owner_summaries.items(), key=lambda item: item[1]['time_adj'], reverse=True):
         d.append([
            owner,
            owner_parent_mapping.get(owner, '<unknown>'),
            float(data['users']),
            float(data['jobs']),
            float(round(data['time'] / float(3600))),
            percent(data['time'] * inv_total_time),
            float(round(data['time_adj'] / float(3600))),
            percent(data['time_adj'] * inv_total_time),
            percent(data['time_adj'] / time_adj),
         ])

      d.append([
         'TOTALS',
         '-',
         float(len(users)), # Note: unique users - not the sum of entries in column
         float(reduce((lambda x, k: x + owner_summaries[k]['jobs']), owner_summaries, 0)),
         float(reduce((lambda x, k: x + owner_summaries[k]['time']), owner_summaries, 0) / float(3600)),
         percent(reduce((lambda x, k: x + owner_summaries[k]['time'] * inv_total_time), owner_summaries, 0)),
         float(reduce((lambda x, k: x + owner_summaries[k]['time_adj']), owner_summaries, 0) / float(3600)),
         percent(reduce((lambda x, k: x + owner_summaries[k]['time_adj'] * inv_total_time), owner_summaries, 0)),
         percent(reduce((lambda x, k: x + owner_summaries[k]['time_adj'] / time_adj), owner_summaries, 0)),
      ])

      print(tabulate(d, headers=h, floatfmt=",.0f"),"\n")

   if 'users' in reports:
      print("==========")
      print("Top users:")
      print("==========\n")

      print_simplestats(users, args.limitusers)

      h = [ 'Usr', 'Owner(s)', 'Jobs', 'Core Hrs', '%Utl', 'Adj Core Hrs', '%Utl', '%Usg' ]
      d = []
      count = 0
      for user, data in sorted(users.items(), key=lambda item: item[1]['time_adj'], reverse=True):
         count += 1
         if count > args.limitusers: break
         d.append([
            user,
            ",".join(sorted([o  for o in owners for u in owners[o] if u == user])),
            float(data['jobs']),
            float(round(data['time'] / float(3600))),
            percent(data['time'] * inv_total_time),
            float(round(data['time_adj'] / float(3600))),
            percent(data['time_adj'] * inv_total_time),
            percent(data['time_adj'] / time_adj),
         ])

      d.append([
         'TOTALS',
         '-',
         float(reduce((lambda x, k: x + users[k]['jobs']), users, 0)),
         float(reduce((lambda x, k: x + users[k]['time']), users, 0) / float(3600)),
         percent(reduce((lambda x, k: x + users[k]['time'] * inv_total_time), users, 0)),
         float(reduce((lambda x, k: x + users[k]['time_adj']), users, 0) / float(3600)),
         percent(reduce((lambda x, k: x + users[k]['time_adj'] * inv_total_time), users, 0)),
         percent(reduce((lambda x, k: x + users[k]['time_adj'] / time_adj), users, 0)),
      ])

      print(tabulate(d, headers=h, floatfmt=",.0f"),"\n")

   if 'usersbyowner' in reports:
      print("===================")
      print("Top users by owner:")
      print("===================\n")

      h = [ 'Usr', 'Jobs', 'Core Hrs', '%Utl', 'Adj Core Hrs', '%Utl', '%Usg' ]

      for owner in sorted(owners):
         time_adj = reduce((lambda x, k: x + owners[owner][k]['time_adj']), owners[owner], 0)

         print("Owner:", owner)
         print_simplestats(owners[owner], args.limitusers)
         d = []
         count = 0
         for user, data in sorted(owners[owner].items(), key=lambda item: item[1]['time_adj'], reverse=True):
            count += 1
            if count > args.limitusers: break
            d.append([
               user,
               float(data['jobs']),
               float(round(data['time'] / float(3600))),
               percent(data['time'] * inv_total_time),
               float(round(data['time_adj'] / float(3600))),
               percent(data['time_adj'] * inv_total_time),
               percent(data['time_adj'] / time_adj),
            ])

         d.append([
            'TOTALS',
            float(reduce((lambda x, k: x + owners[owner][k]['jobs']), owners[owner], 0)),
            float(reduce((lambda x, k: x + owners[owner][k]['time']), owners[owner], 0) / float(3600)),
            percent(reduce((lambda x, k: x + owners[owner][k]['time'] * inv_total_time), owners[owner], 0)),
            float(reduce((lambda x, k: x + owners[owner][k]['time_adj']), owners[owner], 0) / float(3600)),
            percent(reduce((lambda x, k: x + owners[owner][k]['time_adj'] * inv_total_time), owners[owner], 0)),
            percent(reduce((lambda x, k: x + owners[owner][k]['time_adj'] / time_adj), owners[owner], 0)),
         ])

         print(tabulate(d, headers=h, floatfmt=",.0f"),"\n")


def print_simplestats(data, top_n):
#   # Rewrite with reduce
#   top_usage = 0
#   for e in enumerate(sorted(data.items(), key=lambda item: item[1]['time_adj'], reverse=True)):
#      if e[0] >= top_n: break
#      top_usage += e[1][1]['time_adj']

#   bottom_usage = 0
#   bottom_n = 0
#   for e in enumerate(sorted(data.items(), key=lambda item: item[1]['time_adj'])):
#      bottom_usage += e[1][1]['time_adj']
#      if bottom_usage > top_usage:
#         bottom_n = max(0, e[0] -1)
#         break

#   if top_n <= len(data):
#      print(
#         len(data),"active users.",
#         "Top", top_n, "("+percent(top_n/len(data))+")",
#         "active users have used more than the bottom",
#         bottom_n, "("+percent(bottom_n/len(data))+")", "combined",
#      )
#   else:
#      print(len(data),"active users.", "Top", top_n, "skipped")

   print(len(data),"active users.")


def percent(num):
   return "{0:.1%}".format(float(num))


# Calculate the number of core hours used by a job
def core_hours(record):
   # Simple (wallclock times cores)
   time = record['ru_wallclock'] * record['slots']

   # Complicated (adjusted for large memory requests)
   time_adj = time

   # - obtain node memory per core
   mem_core = None
   nt = sge.category_resource(record['category'], 'node_type')
   if nt:
      cores  = sge.number(sge.node_type(nt, 'num_pe'))
      memory = sge.number(sge.node_type(nt, 'memory'))

      if cores and memory:
         mem_core = memory // cores

   # - backup method of figuring out node memory per core
   if not mem_core:
      # Cycle through node name regexs for a match
      for b in backup_node_mpc:
         r = b['re'].match(record['hostname'])
         if r:
            mem_core = b['mpc']

   # - obtain memory request
   mem_req = sge.category_resource(record['category'], 'h_vmem')
   if mem_req:
      mem_req = sge.number(mem_req)

   if mem_req is not None and mem_core is not None:
      time_adj *= math.ceil(mem_req / float(mem_core))
      #time_adj *= max(1, mem_req / float(mem_core))
   else:
      print("Warning: could not extract mem or mem per node details for", record['name'],"("+record['category']+")", file=sys.stderr)

   return time, time_adj


# Take a date range string of format [DATE][-[DATE]], where DATE has format
# YYYY[MM[DD[HH[MM[SS]]]]], and return a tuple with the seconds since the
# epoch bounding the start and end of that range (start - inclusive,
# end - exclusive).
def parse_startend(date_str):
   start = 0
   end = int(datetime.datetime(
      *parse_date(max_date),
      tzinfo=pytz.timezone('UTC'),
   ).strftime('%s'))

   if date_str:
      r = time_startend_def.match(date_str)
      if r:
         if r.group(1):
            start_dt = datetime.datetime(
               *datetime_defaults(*parse_date(r.group(1))),
               tzinfo=pytz.timezone('UTC'),
            )

            start = int(start_dt.strftime('%s'))

         end_dt = next_datetime(
            *parse_date(r.group(3) or (r.group(2) and max_date) or r.group(1)),
            tzinfo=pytz.timezone('UTC'),
         )

         end   = int(end_dt.strftime('%s'))

   return start, end


# Take a date/time string with optional components of format
# YYYY[MM[DD[HH[MM[SS]]]]] and return that information split into a tuple
# as integers
def parse_date(date):
   if date:
      r = datetime_def.match(date)
      if r:
         # Convert strings to integers - don't initialise anything we don't
         # have information for.
         return ( int(e) for e in r.groups() if e != None )

   return None


# Takes similar arguments as datetime, returns a datetime
# object "1" louder, e.g. if args specify a particular month,
# will return the next month in the same year.
def next_datetime(*date_time, tzinfo=pytz.timezone('UTC')):
   t1 = datetime.datetime(*datetime_defaults(*date_time), tzinfo=tzinfo)

   case = {
      1: t1 + relativedelta(years=1),
      2: t1 + relativedelta(months=1),
      3: t1 + datetime.timedelta(days=1),
      4: t1 + datetime.timedelta(hours=1),
      5: t1 + datetime.timedelta(minutes=1),
      6: t1 + datetime.timedelta(seconds=1),
   }

   return case.get(len(date_time))


# Takes a list/tuple of datetime arguments (year, month, etc.), filling
# out with the minimum defaults assuming we're interested in the start
# of a month, year, or the Unix epoch.
def datetime_defaults(*date_time):
   t = list(date_time)

   # datetime needs some minimum information - apply defaults to any missing
   if len(t) < 1: t.append(1970) # year
   if len(t) < 2: t.append(1) # month
   if len(t) < 3: t.append(1) # day

   return tuple(t)


# Run program (if we've not been imported)
# ---------------------------------------

if __name__ == "__main__":
   main()
