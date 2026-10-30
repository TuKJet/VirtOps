#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    virtops_desktop_lib::run().expect("error while running VirtOps Desktop");
}
