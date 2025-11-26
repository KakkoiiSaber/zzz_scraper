from pathlib import Path

from src.scraper.PostScraper import PostScraper
from src.scraper.MinasScraper import MinasScraper
from src.downloader.MinasDownloader import run_batch_from_csv

def main():
    PostScraper("米游社-官方资讯").run()
    PostScraper("官网-新闻资讯").run()
    MinasScraper("米游社-官方资讯-minas").run()
    run_batch_from_csv(
        Path("metafiles/米游社-官方资讯-minas.csv"),
        Path("./downloads")
    )

if __name__ == "__main__":
    main()