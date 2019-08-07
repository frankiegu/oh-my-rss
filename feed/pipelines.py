# -*- coding: utf-8 -*-

# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://doc.scrapy.org/en/latest/topics/item-pipeline.html

from feed.utils import *
from scrapy.exceptions import DropItem
import django
import urllib
from bs4 import BeautifulSoup
import lxml.etree as etree

# to use django models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ohmyrss.settings")
django.setup()

from web.models import *


class ValidPipeline(object):

    def process_item(self, item, spider):

        if item['title'] and item['content'] and item['url'] and item['name']:
            return item
        else:
            raise DropItem(f"Data not valid：{item}")


class DomPipeline(object):
    """
    handle dom structure
    """

    def process_item(self, item, spider):
        content_soup = BeautifulSoup(item['content'], "html.parser")

        # to absolute external href
        for a in content_soup.find_all('a'):
            rel_href = a.attrs.get('href')
            abs_href = urllib.parse.urljoin(item['url'], rel_href)
            a.attrs['href'] = abs_href
            a.attrs['target'] = '_blank'

        # deny exec js
        for script in content_soup.find_all('script'):
            script.name = 'noscript'

        # trim contents
        if item.get('trims'):
            content_etree = etree.fromstring(content_soup.prettify(), etree.HTMLParser())
            for xpath in item['trims']:
                for node in content_etree.xpath(xpath):
                    node.clear()
            item['content'] = etree.tostring(content_etree, pretty_print=True, encoding="utf-8").decode('utf8')
        else:
            item['content'] = content_soup.prettify()

        return item


class InsertDBPipeline(object):

    def process_item(self, item, spider):
        site = Site.objects.get(name=item['name'])

        if site.status == 'active':
            article = Article(site=site, title=item['title'], uindex=current_ts(), content=item['content'],
                              remark='', src_url=item['url'])
            article.save()

            # mark status
            mark_crawled_url(item['url'])