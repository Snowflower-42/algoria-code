from pathlib import Path
from urllib.parse import urljoin
import csv
import re
import time

from playwright.sync_api import sync_playwright


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://empresa.mx.computrabajo.com"

MATCH_URL = (
    "https://empresa.mx.computrabajo.com/Company/Offers/Match"
    "?oi=9337F02A6E4E87E161373E686DCF3405"
    "&cf=469814F59E4D6F04"
)

PROFILE_DIR = Path("./computrabajo_browser")
DOWNLOAD_DIR = Path("./candidate_cvs")
LOG_FILE = DOWNLOAD_DIR / "CV_Database.csv"


PROFILE_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def safe_filename(name):
    """
    Remove characters that Windows does not allow in filenames.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()

    # Windows does not like filenames ending with . or space
    name = name.rstrip(". ")

    return name[:180] or "candidate"


def get_total_candidates(page):
    """
    Read the number from:
        Recibidos (517)

    Returns:
        517
    """

    # First try the specific Recibidos link.
    received_link = page.locator("#link_Recibidos").first

    if received_link.count() > 0:
        text = received_link.inner_text().strip()

        match = re.search(r"Recibidos\s*\(\s*(\d+)\s*\)", text)

        if match:
            return int(match.group(1))

    # Fallback: search the page text.
    body_text = page.locator("body").inner_text()

    match = re.search(
        r"Recibidos\s*\(\s*(\d+)\s*\)",
        body_text
    )

    if match:
        return int(match.group(1))

    raise RuntimeError(
        "Could not find the number of candidates in 'Recibidos (#)'."
    )


def get_candidate_name(page):
    """
    Extract candidate name from:

        Currículum de  Candidate Name
    """

    heading = page.locator("#headerCvDetail h1").first

    if heading.count() == 0:
        raise RuntimeError(
            "Could not find candidate name."
        )

    text = heading.inner_text().strip()

    # Remove "Currículum de"
    text = re.sub(
        r"^\s*Currículum\s+de\s+",
        "",
        text,
        flags=re.IGNORECASE
    ).strip()

    return text


def get_extension(content_type, body, headers):
    """
    Determine whether the downloaded file is PDF or DOCX.

    Priority:
    1. Content-Disposition filename
    2. Content-Type
    3. File signature
    """

    # --------------------------------------------------------
    # 1. Check Content-Disposition
    # --------------------------------------------------------

    content_disposition = headers.get(
        "content-disposition",
        ""
    )

    match = re.search(
        r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)",
        content_disposition,
        re.IGNORECASE
    )

    if match:
        filename = (
            match.group(1)
            or match.group(2)
            or ""
        ).lower()

        if filename.endswith(".pdf"):
            return ".pdf"

        if filename.endswith(".docx"):
            return ".docx"

        if filename.endswith(".doc"):
            return ".doc"

    # --------------------------------------------------------
    # 2. Check Content-Type
    # --------------------------------------------------------

    content_type = headers.get(
        "content-type",
        ""
    ).lower()

    if "application/pdf" in content_type:
        return ".pdf"

    if (
        "wordprocessingml" in content_type
        or "application/vnd.openxmlformats-officedocument"
        in content_type
    ):
        return ".docx"

    if "application/msword" in content_type:
        return ".doc"

    # --------------------------------------------------------
    # 3. Check file signature
    # --------------------------------------------------------

    # PDF files begin with %PDF
    if body[:4] == b"%PDF":
        return ".pdf"

    # DOCX files are ZIP containers and normally begin PK
    if body[:2] == b"PK":
        return ".docx"

    return None


def save_cv(
    context,
    candidate_name,
    download_url
):
    """
    Download the original CV attachment and save it as:

        Candidate Name_CV.pdf

    or

        Candidate Name_CV.docx

    Existing files with the same name are overwritten.
    """

    response = context.request.get(
        download_url,
        timeout=60000
    )

    if not response.ok:
        raise RuntimeError(
            f"HTTP {response.status}"
        )

    content_type = (
        response.headers
        .get("content-type", "")
        .lower()
    )

    body = response.body()

    # --------------------------------------------------------
    # Detect expired session / login page
    # --------------------------------------------------------

    if "text/html" in content_type:

        preview = body[:500].decode(
            "utf-8",
            errors="ignore"
        )

        raise RuntimeError(
            "Computrabajo returned HTML instead of the CV. "
            "The login session may have expired.\n"
            f"Response begins: {preview[:200]}"
        )

    # --------------------------------------------------------
    # Determine file extension
    # --------------------------------------------------------

    extension = get_extension(
        content_type,
        body,
        response.headers
    )

    if extension is None:
        raise RuntimeError(
            "Could not determine whether the CV is PDF or DOCX. "
            f"Content-Type: {content_type}"
        )

    if extension not in [".pdf", ".docx", ".doc"]:
        raise RuntimeError(
            f"Unsupported CV file type: {extension}"
        )

    # --------------------------------------------------------
    # Build requested filename
    # --------------------------------------------------------

    filename = (
        f"{safe_filename(candidate_name)}_CV{extension}"
    )

    output_path = DOWNLOAD_DIR / filename

    # --------------------------------------------------------
    # OVERWRITE existing file
    # --------------------------------------------------------

    output_path.write_bytes(body)

    return output_path


def log_result(row):
    """
    Append result to CSV log.
    """

    exists = LOG_FILE.exists()

    with LOG_FILE.open(
        "a",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=[
                "number",
                "candidate",
                "detail_url",
                "download_url",
                "file",
                "status",
                "error",
            ]
        )

        if not exists:
            writer.writeheader()

        writer.writerow(row)


# ============================================================
# MAIN PROGRAM
# ============================================================

with sync_playwright() as p:

    # --------------------------------------------------------
    # Open persistent browser profile
    # --------------------------------------------------------

    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        accept_downloads=True,
    )

    page = (
        context.pages[0]
        if context.pages
        else context.new_page()
    )

    print()
    print("Opening Computrabajo...")
    print()

    page.goto(
        MATCH_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    # --------------------------------------------------------
    # Manual login
    # --------------------------------------------------------

    print("If Computrabajo asks you to log in,")
    print("log in manually in the browser.")
    print()
    print(
        "Once the candidate list is visible, "
        "press ENTER here."
    )

    input()

    # --------------------------------------------------------
    # Read total candidate count
    # --------------------------------------------------------

    total_candidates = get_total_candidates(page)

    print()
    print(
        f"Computrabajo reports "
        f"{total_candidates} candidates in Recibidos."
    )
    print()

    # --------------------------------------------------------
    # Find the first candidate
    # --------------------------------------------------------

    candidate_links = page.locator(
        'a[href*="/Company/MatchCvDetail/MatchDetail"]'
    )

    if candidate_links.count() == 0:

        raise RuntimeError(
            "Could not find any candidate links "
            "on the Recibidos page."
                                         
             
        )

    first_candidate_url = candidate_links.first.get_attribute(
        "href"
    )
                                                   

    if not first_candidate_url:

        raise RuntimeError(
            "The first candidate does not have a detail URL."
        )

    first_candidate_url = urljoin(
        BASE_URL,
        first_candidate_url
    )

    print(
        "Opening first candidate..."
    )

    page.goto(
        first_candidate_url,
        wait_until="domcontentloaded",
        timeout=60000
    )

    # --------------------------------------------------------
    # Process candidates one by one
    # --------------------------------------------------------

    for number in range(
        1,
        total_candidates + 1
    ):

        print()
        print(
            "=" * 70
                     
        )
        print(
            f"Candidate {number} / {total_candidates}"
        )
        print(
            "=" * 70
        )

        try:

            # Wait for candidate detail page
            page.wait_for_selector(
                "#headerCvDetail",
                                              
                timeout=30000
            )

            # ------------------------------------------------
            # Candidate name
            # ------------------------------------------------

            candidate_name = get_candidate_name(page)

            print(
                f"Candidate: {candidate_name}"
            )

            # ------------------------------------------------
            # Current detail URL
            # ------------------------------------------------

            detail_url = page.url

            # ------------------------------------------------
            # Find original CV attachment
            # ------------------------------------------------

            download_link = page.locator(
                "a.js_download_file"
            ).first

            if download_link.count() == 0:
                                                       

                            
                                      
                                             
                                       
                               
                                                   
                                
                  

                        

                                                              

                print(
                    "No attached CV found."
                )

                log_result({
                    "number": number,
                    "candidate": candidate_name,
                    "detail_url": detail_url,
                    "download_url": "",
                    "file": "",
                    "status": "NO_ATTACHMENT",
                    "error": "",
                })

            else:

                download_url = (
                    download_link.get_attribute("href")
                )

                if not download_url:

                    print(
                        "CV attachment link has no URL."
                    )
                                                                 
                                                              
                                                                  

                    log_result({
                        "number": number,
                        "candidate": candidate_name,
                        "detail_url": detail_url,
                        "download_url": "",
                        "file": "",
                        "status": "NO_DOWNLOAD_URL",
                        "error": "",
                    })

                else:
                                   
                                             
                 

                    download_url = urljoin(
                        BASE_URL,
                        download_url
                    )

                    print(
                        "Downloading original CV..."
                    )

                    output_path = save_cv(
                        context,
                        candidate_name,
                        download_url
                                   
                    )

                    print(
                        f"Saved: {output_path.name}"
                                                     
                                                       
                    )

                    log_result({
                        "number": number,
                        "candidate": candidate_name,
                        "detail_url": detail_url,
                        "download_url": download_url,
                        "file": str(output_path),
                        "status": "OK",
                        "error": "",
                    })

        except Exception as e:

            print(
                f"ERROR: {e}"
                              
            )

            log_result({
                "number": number,
                "candidate": (
                    candidate_name
                    if "candidate_name" in locals()
                    else ""
                ),
                "detail_url": page.url,
                "download_url": "",
                "file": "",
                "status": "ERROR",
                "error": str(e),
            })

        # ----------------------------------------------------
        # Go to next candidate
        # ----------------------------------------------------

        if number >= total_candidates:
                                       

            print()
            print(
                "Reached the number of candidates "
                "reported by Recibidos."
            )

            break
                                      

        try:

            next_button = page.locator(
                "#js-next"
            ).first
                                           

            if next_button.count() == 0:

                raise RuntimeError(
                    "Next candidate button (#js-next) "
                    "was not found."
                                             
                )

            next_url = next_button.get_attribute(
                "href"
            )

            if not next_url:

                raise RuntimeError(
                    "Next candidate button has no URL."
                )

            next_url = urljoin(
                BASE_URL,
                next_url
            )

            print(
                "Moving to next candidate..."
            )

            # Use the actual Next candidate link.
            page.goto(
                next_url,
                wait_until="domcontentloaded",
                timeout=60000
            )
                            
              

            page.wait_for_selector(
                "#headerCvDetail",
                timeout=30000
            )

        except Exception as e:

            print()
            print(
                "STOPPED: Could not move to the next candidate."
            )
            print(
                f"Reason: {e}"
            )
            print()

            break
                                  
                                         
                                   
                           
                                  
                                
              

        time.sleep(0.5)

    # --------------------------------------------------------
    # Finished
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("FINISHED")
    print("=" * 70)
    print()
    print(
        f"CV folder: {DOWNLOAD_DIR.resolve()}"
    )
    print(
        f"Log file:  {LOG_FILE.resolve()}"
    )
    print()

    context.close()
