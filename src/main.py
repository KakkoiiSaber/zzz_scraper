from pathlib import Path

from scraper.PostScraper import PostScraper
from scraper.MinasScraper import MinasScraper
from downloader.MinasDownloader import run_batch_from_csv


PostScraper("米游社-官方资讯").run()
PostScraper("官网-新闻资讯").run()
MinasScraper("米游社-官方资讯-minas").run()
run_batch_from_csv(
    Path("metafiles/米游社-官方资讯-minas.csv"),
    Path("./downloads")
)