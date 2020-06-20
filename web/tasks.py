from __future__ import absolute_import, unicode_literals

from celery import shared_task
from web.models import *
import logging
from datetime import datetime
from django.utils.timezone import timedelta
from web.omrssparser.atom import atom_spider
from web.omrssparser.wemp import parse_wemp_ershicimi
from web.utils import is_active_rss, set_similar_article, get_similar_article, cal_cosine_distance, \
    get_user_sub_feeds, set_feed_ranking_dict, write_dat_file, is_updated_site, add_referer_stats, get_host_name
import jieba
from web.stopwords import stopwords
from bs4 import BeautifulSoup
from collections import Counter
import json

logger = logging.getLogger(__name__)


@shared_task
def update_sites_async(site_list, force_update=False):
    """
    异步更新某一批订阅源
    """
    for site_name in site_list:
        try:
            site = Site.objects.get(status='active', name=site_name)
        except:
            continue

        # 最近已经更新过了，跳过
        if not force_update and is_updated_site(site_name):
            continue

        if site.creator == 'user':
            atom_spider(site)
        elif site.creator == 'wemp':
            # 公众号不异步更新，因为是同一个站点，控制请求频率，只有定时任务触发
            # atom_spider(site)
            pass

    return True


@shared_task
def update_all_atom_cron():
    """
    定时更新所有源，1～2 小时的周期
    """
    now, sites = datetime.now(), []

    # 按照不同频率更新，区分用户自定义的和推荐的
    if now.hour % 2 == 0:
        sites = Site.objects.filter(status='active', creator='user').order_by('-star')
    elif now.hour % 2 == 1:
        sites = Site.objects.filter(status='active', creator='user', star__gte=9).order_by('-star')

    for site in sites:
        # 无人订阅的源且不推荐的源不更新
        if not is_active_rss(site.name) and site.star < 9:
            continue

        # 是否已经更新过
        if not is_updated_site(site.name):
            atom_spider(site)

    return True


@shared_task
def update_all_wemp_cron():
    """
    更新微信公众号，每天 1～2 次
    """
    sites = Site.objects.filter(status='active', creator='wemp').order_by('-star')

    for site in sites:
        host = get_host_name(site.rss)

        if 'ershicimi.com' in host:
            parse_wemp_ershicimi(site.rss, update=True)
        elif 'qnmlgb.tech' in host:
            atom_spider(site)
        else:
            pass

    return True


@shared_task
def archive_article_cron():
    """
    归档并清理文章，每天一次
    """
    # (, 10)，直接删除
    Article.objects.filter(site__star__lt=10, is_recent=False).delete()

    # [10, 20)，转移存储到磁盘
    articles = Article.objects.filter(site__star__gte=10, is_recent=False).order_by('-id').iterator()

    for article in articles:
        if not article.content.strip():
            break

        if write_dat_file(article.uindex, article.content):
            article.content = ' '
            article.save()

    return True


@shared_task
def cal_all_article_tag_cron():
    """
    设置最近一周的文章标识、统计词频；每隔 ？ 分钟
    """
    # 设置最近一周文章标识
    lastweek = datetime.now() - timedelta(days=7)

    Article.objects.filter(is_recent=True, ctime__lte=lastweek).update(is_recent=False)

    # 扫描空 tag 然后统计词频
    articles = Article.objects.filter(is_recent=True, status='active', site__star__gte=10, tags='').order_by('-id')

    for article in articles:
        content_soup = BeautifulSoup(article.content, 'html.parser')
        content_text = content_soup.get_text() + 3 * article.title
        seg_list = jieba.cut(content_text)
        words_list = []

        for seg in seg_list:
            seg = seg.strip().lower()

            if len(seg) > 1 and seg not in stopwords:
                words_list.append(seg)

        tags_list = dict(Counter(words_list).most_common(10))

        # 过滤只出现一次的
        tags_list = {k: v for k, v in tags_list.items() if v >= 2}

        if tags_list:
            logger.info(f'{article.uindex}`{tags_list}')

            article.tags = json.dumps(tags_list)
            article.save()

    return True


@shared_task
def cal_article_distance_cron():
    """
    计算新增文章的相似度，用于推荐订阅；每隔 ？ 分钟计算一次
    """
    articles = Article.objects.filter(is_recent=True, status='active', site__star__gte=10).exclude(tags='').\
        order_by('-id')

    lastmonth = datetime.now() - timedelta(days=30)

    for article in articles:
        similar_dict = {}

        if not get_similar_article(article.uindex):
            # 和过去一个月的文章对比
            compare_articles = Article.objects.filter(status='active', site__star__gte=10, ctime__gt=lastmonth).\
                exclude(tags='').values('uindex', 'tags')

            for compare in compare_articles:
                src_tags = json.loads(article.tags)
                dest_tags = json.loads(compare['tags'])
                distance = cal_cosine_distance(src_tags, dest_tags)

                if 0.1 < distance < 1:
                    similar_dict[compare['uindex']] = distance

            if similar_dict:
                sorted_similar_dict = dict(sorted(similar_dict.items(), key=lambda v: v[1], reverse=True)[:10])

                logger.info(f'{article.uindex}`{sorted_similar_dict}')

                set_similar_article(article.uindex, sorted_similar_dict)

    return True


@shared_task
def cal_site_ranking_cron():
    """
    计算订阅排行榜单 top 100，每天 1 次
    """
    users = User.objects.all().values_list('oauth_id', flat=True)

    all_user_feeds = []
    dest_feed_ranking = []

    for oauth_id in users:
        all_user_feeds += get_user_sub_feeds(oauth_id, from_user=False)

    feed_ranking = dict(Counter(all_user_feeds).most_common(100))

    for (feed, score) in feed_ranking.items():
        try:
            site_dict = Site.objects.get(name=feed).__dict__
            del site_dict['_state'], site_dict['ctime'], site_dict['mtime'], site_dict['creator'], site_dict['rss'], \
                site_dict['status'], site_dict['copyright'], site_dict['tag'], site_dict['remark'], site_dict['freq']
            site_dict['score'] = score
        except:
            logger.warning(f"订阅源不存在：`{feed}")
            continue

        dest_feed_ranking.append(site_dict)

    set_feed_ranking_dict(dest_feed_ranking)

    return True


@shared_task
def add_referer_stats_async(referer):
    """
    站点引流的统计数据
    """
    add_referer_stats(referer)
    return True