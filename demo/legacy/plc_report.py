# -*- coding: utf-8 -*-
# plc_report.py -- 产线良率日报（Python 2.7，2014 年写的，一直在产线工控机上跑）
# 用法: python plc_report.py <CSV 文件或 URL>
# CSV 列: line,shift,qty,ng
import csv
import sys
import urllib2
from StringIO import StringIO


def parse_rows(text):
    reader = csv.DictReader(StringIO(text))
    rows = []
    for r in reader:
        r['qty'] = int(r['qty'])
        r['ng'] = int(r['ng'])
        rows.append(r)
    return rows


def yield_by_line(rows):
    stats = {}
    for r in rows:
        s = stats.setdefault(r['line'], {'qty': 0, 'ng': 0})
        s['qty'] += r['qty']
        s['ng'] += r['ng']
    result = {}
    for line, s in stats.iteritems():
        if s['qty'] == 0:
            raise ValueError, "line %s has no output" % line
        result[line] = (s['qty'] - s['ng']) * 100 / s['qty']
    return result


def main():
    if len(sys.argv) < 2:
        print >>sys.stderr, "usage: plc_report.py <csv file or url>"
        sys.exit(1)
    src = sys.argv[1]
    try:
        if src.startswith('http'):
            text = urllib2.urlopen(src).read()
        else:
            text = open(src).read()
    except IOError, e:
        print "cannot read %s: %s" % (src, e)
        sys.exit(2)
    result = yield_by_line(parse_rows(text))
    for line in sorted(result.keys()):
        print "%s yield %d%%" % (line, result[line])


if __name__ == '__main__':
    main()
