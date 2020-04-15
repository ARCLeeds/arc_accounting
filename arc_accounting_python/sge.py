# Python library providing useful Gridengine functions

import os
import re
import sys
import syslog

# DEBUG:
# - each regex definition should just be in the scope of, and near to,
#   the function using it.
# - need to embed function documentation in a pythonic way, rather
#   than as comments

record_def = re.compile(r"""
   (?P<qname>[^:#]+)
   :(?P<hostname>[^:]+)
   :(?P<grp>[^:]+)
   :(?P<owner>[^:]+)
   :(?P<job_name>[^:]+)
   :(?P<job_number>[^:]+)
   :(?P<account>[^:]+)
   :(?P<priority>[^:]+)
   :(?P<submission_time>[^:]+)
   :(?P<start_time>[^:]+)
   :(?P<end_time>[^:]+)
   :(?P<failed>[^:]+)
   :(?P<exit_status>[^:]+)
   :(?P<ru_wallclock>[^:]+)
   :(?P<ru_utime>[^:]+)
   :(?P<ru_stime>[^:]+)
   :(?P<ru_maxrss>[^:]+)
   :(?P<ru_ixrss>[^:]+)
   :(?P<ru_ismrss>[^:]+)
   :(?P<ru_idrss>[^:]+)
   :(?P<ru_isrss>[^:]+)
   :(?P<ru_minflt>[^:]+)
   :(?P<ru_majflt>[^:]+)
   :(?P<ru_nswap>[^:]+)
   :(?P<ru_inblock>[^:]+)
   :(?P<ru_oublock>[^:]+)
   :(?P<ru_msgsnd>[^:]+)
   :(?P<ru_msgrcv>[^:]+)
   :(?P<ru_nsignals>[^:]+)
   :(?P<ru_nvcsw>[^:]+)
   :(?P<ru_nivcsw>[^:]+)
   :(?P<project>[^:]+)
   :(?P<department>[^:]+)
   :(?P<granted_pe>[^:]+)
   :(?P<slots>[^:]+)
   :(?P<task_number>[^:]+)
   :(?P<cpu>[^:]+)
   :(?P<mem>[^:]+)
   :(?P<io>[^:]+)
   :(?P<category>.*)   # Warning - can contain ":"'s
   :(?P<iow>[^:]+)
   :(?P<pe_taskid>[^:]+)
   :(?P<maxvmem>[^:]+)
   :(?P<arid>[^:]+)
   :(?P<ar_sub_time>[^:]+)
   $
""", re.VERBOSE)

alloc_def = re.compile(r"""
   sgealloc
   \s+cluster=\S+
   \s+job=(?P<job_number>\d+)\.(?P<task_number>\S+)
   \s+(?P<alloc>\S+)
""", re.VERBOSE)

host_def = re.compile(r"""
   \S+@(\S+)=\d+
""", re.VERBOSE)

host_prune = re.compile(r"[^.]+")

number_suffix_def = re.compile(r"^([0-9.]+)(\D+)$")
number_time_def   = re.compile(r"^(\d+):(\d+):(\d+)$")

node_type_def = re.compile(r"""
   ^(?P<num_pe>\d+)
   (?P<pe_type>core|thread)-
   (?P<memory>[^-]+)
   (-(?P<coproc>[^-]+))?
""", re.VERBOSE)


# Try to detect any compression and open appropriately
def open_file(file):
   if file.endswith('.gz'):
      import gzip
      return gzip.open(file, 'rt')
   elif file.endswith('.bz2'):
      import bz2
      return bz2.open(file, 'rt')
   else:
      return open(file, 'r')


# Generator
# Walks all accounting records, returning a dictionary per record
# Allows retrieval of all records, or just one at a time.
def records(accounting = None,
            filter = None,
            modify = None,
          ):

   # If given no datasource, look at default location
   if accounting == None:
      accounting = os.environ["SGE_ROOT"] + "/" + os.environ["SGE_CELL"] + "/common/accounting"

   # Iterate non-strings directly, as we may be tailing a stram
   # of accounting data where the parent has open/close control
   if type("") == type(accounting):
      f = open_file(accounting)
   else:
      f = accounting

   for line in f:
      try:
         r = record_def.match(line)
         if r:
            d = r.groupdict()
 
            # Create a combined job/task name
            d['name'] = d['job_number'] + "." + ('1' if d['task_number'] == '0' else d['task_number'])
 
            # Prune DNS domainname (most SGE installations are domainname-insensitive)
            d['hostname'] = host_prune.match(d['hostname']).group()
 
            # Convert integer fields from strings to integers
            for f in [
                        'job_number',
                        'submission_time',
                        'start_time',
                        'end_time',
                        'failed',
                        'exit_status',
                        'slots',
                        'task_number',
                        'arid',
                        'ar_sub_time',
                     ]:
               d[f] = int(d[f])
 
            # Convert float fields from strings to floats
            for f in [
                        'priority',
                        'ru_wallclock',
                        'ru_utime',
                        'ru_stime',
                        'ru_maxrss',
                        'ru_ixrss',
                        'ru_ismrss',
                        'ru_idrss',
                        'ru_isrss',
                        'ru_minflt',
                        'ru_majflt',
                        'ru_nswap',
                        'ru_inblock',
                        'ru_oublock',
                        'ru_msgsnd',
                        'ru_msgrcv',
                        'ru_nsignals',
                        'ru_nvcsw',
                        'ru_nivcsw',
                        'cpu',
                        'mem',
                        'io',
                        'iow',
                        'maxvmem',
                     ]:
               d[f] = float(d[f])
 
            # Modify record, e.g. add extra fields
            if modify: modify(d)
 
            # Filter out undesirable records
            if filter:
               if not filter(d): continue
 
            # Return record
            yield(d)
      except:
         syslog.syslog("Failed to process one record" + str(sys.exc_info()))

# Generator
# Walks all accounting records, returning a dictionary per record
# Allows retrieval of all records, or just one at a time.
def dbrecords(db, service, filter_spec=None, fields=('*', ), modify=None):
   import MySQLdb as mariadb
   cursor = db.cursor(mariadb.cursors.SSDictCursor)

   # Lookup serviceid

   serviceid = -1
   cursor.execute("SELECT id FROM services WHERE name = %s", (service,))
   for d in cursor:
      serviceid = d['id']

   # Generate query

   select = "SELECT " + ", ".join(fields) + \
            " FROM sge, jobs" + \
            " WHERE "

   where = [ "sge.job=jobs.job", "sge.serviceid = %s", "jobs.serviceid = %s" ]
   values = [ serviceid, serviceid ]
   for sp in filter_spec:
      for f, act in sp.items():
         for op, vals in act.items():
            if op == '==' or op == '=':
               conj = " OR "
            else:
               conj = " AND "

            values.extend(vals)
            where.append("("+ conj.join([f +" "+ op + " %s"]*len(vals)) +")")

   select += " AND ".join(where)

   # Execute query

   cursor.execute(select, values)

   # Modify and return records

   for d in cursor:
      # Modify record, e.g. add extra fields
      if modify: modify(d)

      # Return record
      yield(d)

   cursor.close()


# Generator
# Walks all job compute node allocation records, returning a dictionary per record
# Allows retrieval of all records, or just one at a time.
def allocs(allocs = "/var/log/local2"):
   for line in open(allocs):
      r = alloc_def.search(line)
      if r:
         d = r.groupdict()
         d['hosts'] = [ host_def.search(h).group(1) for h in d['alloc'].split(",") ]
         d['name'] = d['job_number'] + "." + d['task_number']
         yield(d)


# Expand a number potentially using gridengine numeric suffixes to a
# simple integer
def number(num):
   if num:
      # Suffix to expand?
      r = number_suffix_def.match(str(num))
      if r:
         for e in enumerate(["K", "M", "G", "T"], start=1):
            if e[1] == r.group(2): return int(float(r.group(1))*1024**int(e[0]))

         for e in enumerate(["k", "m", "g", "t"], start=1):
            if e[1] == r.group(2): return int(float(r.group(1))*1000**int(e[0]))

      # Time to expand?
      r = number_time_def.match(str(num))
      if r:
         return int(r.group(1))*3600 + int(r.group(2))*60 + int(r.group(3))

      return int(num)

   return None

#DEBUG - not working yet. Will simplify an integer using gridengine
# size suffixes.
def contract_number(num):
   True

#DEBUG - not working yet. Initial attempt to migrate some perl code for
# contract_number - rethink this in python.
# WARNING: python2 division treat this differently?
def find_suffix(num, base):
   val = num / base
   print("test",val, num // base)
   if val != 0 and val == (num // base):
      print("recurse...")
      val, nbase = find_suffix(val, base)
      print("2",val,nbase)
      return val, nbase+1
   else:
      return val, 0

# When supplied an accounting "category" string, return a specified
# resource request
def category_resource(category, resource):
   swtch = False
   for c in category.split(' '):
      if swtch:
         # Extract the resource request
         for r in c.split(','):
            d = r.split('=')
            if d[0] == resource:
               if d[0] == 'h_vmem' or d[0] == 'h_rt':
                  return number(d[1])
               else:
                  return d[1]
         swtch = False

      # Find the start of a resource request string
      if c == '-l': swtch = True

   return 0

# When supplied a node_type string, extract a given component
# (num_pe, pe_type, memory, coproc)
def node_type(nodetype, element):
   r = node_type_def.match(nodetype)
   if r:
      d = r.groupdict()
      if element in d:
         return d[element]


# Generic SQL helper (not really related to SGE... need better home)
# Return record (after creating or updating)
# - select: execute this to return records
# - update: if found records, execute this
# - insert: if no records, execute this
# - oninsert: if inserted records, also execute this (e.g. other table)
def sql_get_create(cursor, select, data, insert=None, update=None, oninsert=None, first=False):
   cursor.execute(select, data)
   sql = cursor.fetchall()

   if len(sql) < 1:
      if insert:
         cursor.execute(insert, data)
         if oninsert: cursor.execute(oninsert, data)

         cursor.execute(select, data)
         sql = cursor.fetchall()
   elif update:
      cursor.execute(update, data)

      cursor.execute(select, data)
      sql = cursor.fetchall()

   # If only want one, don't return record embedded in a tuple
   if first:
      if len(sql) > 0: return sql[0]
      return None

   return sql


def dbgetfield(db, select, data):
   import MySQLdb as mariadb
   cursor = db.cursor()

   field = None
   cursor.execute(select, data)
   for d in cursor:
      field = d[0]

   return field


def dbgetfields(db, select, data):
   import MySQLdb as mariadb
   cursor = db.cursor(mariadb.cursors.SSDictCursor)

   field = None
   cursor.execute(select, data)
   for d in cursor:
      field = d

   return field


# Tidy/close database connection
def dbtidy(db):
   try:
      db.close()
   except:
      None


def dbavail(db, service, start, end, queues, skipqueues):
   serviceid = dbgetfield(db, "SELECT id FROM services WHERE name = %s", (service,))

   # total - total number of slot seconds
   # avail - number of slot seconds available, counting reservations as not available
   # avail_usrrsv - number of slots seconds available, counting reservations as available
   fields = (
      "SUM(slots_total*ttl) AS total",
      "SUM((slots_total*enabled + (slots_total-GREATEST(slots_used, slots_reserved))*(1-enabled))*available*ttl) AS avail",
      "SUM((slots_total*enabled + (slots_total-slots_used)*(1-enabled))*available*ttl) AS avail_usrrsv",
   )

   select = "SELECT " + ",".join(fields) + " FROM availability WHERE serviceid = %s AND time > %s AND time <= %s"

   data = [ serviceid, start, end ]

   if queues:
      select += " AND ("

      num = 0
      for q in queues:
         queueid = dbgetfield(db, "SELECT id FROM queues WHERE serviceid = %s AND name = %s", (serviceid, q))
         if queueid:
            if num > 0: select += " OR "
            select += "queueid = %s"
            data.append(queueid)
            num = num +1

      select += ")"

   if skipqueues:
      for q in skipqueues:
         queueid = dbgetfield(db, "SELECT id FROM queues WHERE serviceid = %s AND name = %s", (serviceid, q))
         if queueid:
            select += " AND queueid != %s"
            data.append(queueid)

   return dbgetfields(db, select, data)
