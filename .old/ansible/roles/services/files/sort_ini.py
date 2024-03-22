#!/usr/bin/python
import sys

USAGE = 'USAGE:\n\tsort_ini.py file.ini'

def sort_ini(fname):
  """sort .ini file: sorts sections and in each section sorts keys"""
  f = file(fname)
  lines = f.readlines()
  f.close()
  f = file(fname, 'w')
  f.truncate(0)
  section = ''
  subcat = ''
  sections = {}
  for line in lines:
    line = line.strip()
    if line:
      if line.startswith('[['):
        subcat = line
        continue
      if line.startswith('['):
        section = line
        subcat = ''
        continue
      if not sections.has_key(section):
        sections[section] = {}
      if not sections[section].has_key(subcat):
        sections[section][subcat] = []
      sections[section][subcat].append(line)

  if sections:
    sk = sections.keys()
    sk.sort()
    for k in sk:
      vals = sections[k]
      sks = vals.keys()
      sks.sort()
      if k != '':
        f.write(k.strip()+'\n')
      for sk in sks:
        subvals = vals[sk]
        subvals.sort()
        if sk != '':
          f.write(sk.strip()+'\n')
        f.write('\n'.join([v.strip() for v in subvals]))
        f.write('\n')

if len(sys.argv) < 2:
  print USAGE
else:
  sort_ini(sys.argv[1])
