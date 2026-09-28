fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("--version") => println!("{} {}", env!("FIXTURE_NAME"), env!("FIXTURE_VERSION")),
        _ => std::process::exit(2),
    }
}
